import argparse
import base64
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from functools import wraps
from html import escape
from pathlib import Path
from typing import Self, Callable
from urllib.parse import quote as _quote_
from urllib.parse import parse_qs, urljoin, urlparse

import crawleruseragents
from curl_cffi.requests import Session as CffiSession
from curl_cffi.requests import Response as CffiResponse
from curl_cffi.requests import RequestsError
import yaml
from bottle import Bottle, request, response, static_file
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook, DiscordEmbed
from yattag import indent


DEFAULT_FACEBOOK_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36'
SHARE_HEAD_USER_AGENT = 'python-requests/2.32.3'
SHARE_BODY_USER_AGENT = 'Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)'


class FacebedException(Exception):
    reason_code = 'facebed_error'


class NoDataException(FacebedException):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = 'no_data',
        terminal: bool = False,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.terminal = terminal


class UnsupportedRouteException(FacebedException):
    def __init__(self, message: str, *, reason_code: str = 'unsupported_route'):
        super().__init__(message)
        self.reason_code = reason_code


class ShareResolutionException(NoDataException):
    def __init__(
        self,
        message: str,
        response: CffiResponse | None = None,
        count_as_error: bool = False,
        *,
        reason_code: str = 'share_resolution_failed',
        terminal: bool = False,
    ):
        super().__init__(
            message,
            reason_code=reason_code,
            terminal=terminal,
        )
        self.upstream_response = response
        self.count_as_error = count_as_error


class ParseException(FacebedException):
    reason_code = 'parser_error'

    def __init__(self, message: str, html: str = '', url: str = ''):
        super().__init__(message)
        self.html = html
        self.url = url


class UpstreamException(FacebedException):
    reason_code = 'upstream_error'

    def __init__(
        self,
        message: str,
        response: CffiResponse | None = None,
        transport_error: bool | None = None,
    ):
        super().__init__(message)
        self.upstream_response = response
        self.transport_error = (
            response is None if transport_error is None else transport_error
        )
        self.status_code = getattr(response, 'status_code', None)
        self.reason_code = (
            'upstream_transport_error'
            if self.transport_error
            else (
                f'upstream_http_{self.status_code}'
                if self.status_code is not None else 'upstream_error'
            )
        )
        self.retry_after = None
        if response is not None:
            self.retry_after = response.headers.get('Retry-After')


class CFFI:
    impersonate: str = 'chrome146'
    timeout: tuple[float, float] = (5.0, 20.0)
    retry_statuses: set[int] = {429, 500, 502, 503, 504}
    max_attempts: int = 2

    def __init__(self) -> None:
        self._local = threading.local()

    @contextmanager
    def request_scope(self):
        if getattr(self._local, 'session', None) is not None:
            yield self._local.session
            return

        session_headers = JsonParser.get_headers()
        session_headers = {
            key: value
            for key, value in session_headers.items()
            if not key.lower().startswith('sec-ch-')
        }
        session_headers['user-agent'] = DEFAULT_FACEBOOK_USER_AGENT
        session = CffiSession(
            impersonate=self.impersonate,
            default_headers=False,
            headers=session_headers,
            discard_cookies=True,
        )
        self._local.session = session
        self._local.get_cache = {}
        self._local.responses = []
        self._local.last_get_response = None
        self._local.selected_get_response = None
        try:
            yield session
        finally:
            try:
                session.close()
            finally:
                for attr in (
                    'session', 'get_cache', 'responses', 'last_get_response',
                    'selected_get_response',
                ):
                    if hasattr(self._local, attr):
                        delattr(self._local, attr)

    def _get_session(self) -> CffiSession:
        return getattr(self._local, 'session', None)

    @property
    def last_get_response(self) -> CffiResponse | None:
        return getattr(self._local, 'last_get_response', None)

    @property
    def selected_get_response(self) -> CffiResponse | None:
        return getattr(self._local, 'selected_get_response', None)

    def select_response(self, response: CffiResponse | None) -> None:
        if response is not None:
            self._local.selected_get_response = response

    def get(self, url: str, **kwargs) -> CffiResponse:
        return self._request('GET', url, **kwargs)

    def head(self, url: str, **kwargs) -> CffiResponse:
        return self._request('HEAD', url, **kwargs)

    def _request(self, method: str, url: str, **kwargs) -> CffiResponse:
        sess = self._get_session()
        if sess is None:
            with self.request_scope():
                return self._request(method, url, **kwargs)

        method = method.upper()
        check_status = kwargs.pop('_check_status', True)
        retry_status_responses = kwargs.pop('_retry_status_responses', True)
        bypass_cache = kwargs.pop('_bypass_cache', False)
        follow_redirects = kwargs.pop('allow_redirects', True)
        max_redirects = kwargs.pop('max_redirects', 10)
        kwargs.pop('cookies', None)
        headers = {
            key: value
            for key, value in dict(kwargs.pop('headers', {})).items()
            if str(key).lower() != 'cookie'
        }
        kwargs.setdefault('timeout', self.timeout)
        if not isinstance(max_redirects, int):
            raise TypeError('max_redirects must be an integer')
        cache = getattr(self._local, 'get_cache', {})
        def freeze(value):
            if isinstance(value, dict):
                return tuple(sorted((str(key), freeze(item)) for key, item in value.items()))
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if isinstance(value, set):
                return tuple(sorted(freeze(item) for item in value))
            try:
                hash(value)
                return value
            except TypeError:
                return repr(value)

        cache_key = (
            url,
            freeze({
                'headers': headers,
                'allow_redirects': follow_redirects,
                'max_redirects': max_redirects,
                **{key: value for key, value in kwargs.items() if key != 'timeout'},
            }),
        )
        if method == 'GET' and not bypass_cache and cache_key in cache:
            cached = cache[cache_key]
            self._local.last_get_response = cached
            if check_status and cached.status_code >= 400:
                raise UpstreamException(
                    f'Facebook returned HTTP {cached.status_code} for {url}', cached
                )
            return cached

        last_error: RequestsError | None = None
        last_response: CffiResponse | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                current_method = method
                current_url = url
                history: list[CffiResponse] = []
                visited_urls = {url}
                while True:
                    request_kwargs = dict(kwargs)
                    if history:
                        request_kwargs.pop('params', None)
                    request_headers = dict(headers)
                    if request_headers:
                        request_kwargs['headers'] = request_headers
                    request_kwargs['allow_redirects'] = False
                    request_kwargs['max_redirects'] = 0

                    sess.cookies.clear()
                    try:
                        upstream_response = sess.request(
                            current_method,
                            current_url,
                            **request_kwargs,
                        )
                    finally:
                        sess.cookies.clear()

                    last_response = upstream_response
                    upstream_response.cookies.clear()
                    content_bytes = (
                        len(upstream_response.content or b'')
                        if current_method == 'GET' else 0
                    )
                    final_url = str(upstream_response.url)
                    status_code = int(upstream_response.status_code)
                    location = upstream_response.headers.get('Location')
                    if current_url != final_url:
                        logging.info(
                            'upstream %s %s -> %s status=%s redirect_hop=%s bytes=%s attempt=%s',
                            current_method,
                            current_url,
                            final_url,
                            status_code,
                            len(history),
                            content_bytes,
                            attempt,
                        )
                    self._local.responses.append({
                        'method': current_method,
                        'requested_url': current_url,
                        'final_url': final_url,
                        'status': status_code,
                        'bytes': content_bytes,
                        'attempt': attempt,
                    })

                    should_redirect = bool(
                        follow_redirects
                        and location
                        and status_code in {301, 302, 303, 307, 308}
                    )
                    if not should_redirect:
                        upstream_response.history = history
                        upstream_response.redirect_count = len(history)
                        break

                    history.append(upstream_response)
                    if max_redirects >= 0 and len(history) > max_redirects:
                        raise UpstreamException(
                            f'Facebook exceeded {max_redirects} redirects for {url}',
                            upstream_response,
                        )
                    next_url = urljoin(final_url, location)
                    parsed_next_url = urlparse(next_url)
                    if (
                        parsed_next_url.scheme.lower() not in {'http', 'https'}
                        or not parsed_next_url.hostname
                        or parsed_next_url.username is not None
                        or parsed_next_url.password is not None
                    ):
                        raise UpstreamException(
                            f'Facebook returned an unsafe redirect target for {url}',
                            upstream_response,
                        )
                    if next_url in visited_urls:
                        raise UpstreamException(
                            f'Facebook entered a redirect loop for {url}',
                            upstream_response,
                        )
                    visited_urls.add(next_url)
                    current_url = next_url
                    if status_code == 303 and current_method != 'HEAD':
                        current_method = 'GET'
                        for key in ('data', 'json', 'files', 'multipart'):
                            kwargs.pop(key, None)

                if method == 'GET':
                    cache[cache_key] = upstream_response
                    self._local.last_get_response = upstream_response

                if (
                    retry_status_responses
                    and status_code in self.retry_statuses
                    and attempt < self.max_attempts
                ):
                    time.sleep(0.25)
                    continue
                if check_status and status_code >= 400:
                    raise UpstreamException(
                        f'Facebook returned HTTP {status_code} for {url}', upstream_response
                    )
                return upstream_response
            except RequestsError as exc:
                last_error = exc
                logging.warning(
                    'upstream %s %s failed on attempt %s/%s: %s',
                    method, url, attempt, self.max_attempts, exc,
                )
                if attempt < self.max_attempts and method in ('GET', 'HEAD'):
                    time.sleep(0.25)
                    continue
                break

        raise UpstreamException(
            f'Facebook request failed for {url}: {last_error}',
            last_response,
            transport_error=True,
        ) from last_error

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / 'assets'

CONFIG_STR = '''
host: 0.0.0.0
port: 9812
timezone: 7
banned_users: []
notifier_webhook: ''
'''.strip()

config: dict = {}
default_config: dict = yaml.safe_load(io.StringIO(CONFIG_STR))
app: Bottle = Bottle()
cffi = CFFI()


class ServiceMetrics:
    def __init__(self, started_at: float | None = None) -> None:
        self._started_at = time.monotonic() if started_at is None else started_at
        self._requests = 0
        self._errors = 0
        self._lock = threading.Lock()

    def record_request(self) -> None:
        with self._lock:
            self._requests += 1

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            uptime_secs = max(0, int(time.monotonic() - self._started_at))
            return uptime_secs, self._requests, self._errors


service_metrics = ServiceMetrics()

WWWFB = 'https://www.facebook.com'
FACEBOOK_REACTION_EMOJIS = {
    '1635855486666999': '👍',
    '1678524932434102': '❤️',
    '613557422527858': '🤗',
    '115940658764963': '😂',
    '478547315650144': '😮',
    '908563459236466': '😢',
    '444813342392137': '😡',
}
FACEBOOK_REACTION_NAME_IDS = {
    'like': '1635855486666999',
    'love': '1678524932434102',
    'care': '613557422527858',
    'haha': '115940658764963',
    'wow': '478547315650144',
    'sad': '908563459236466',
    'angry': '444813342392137',
}
class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        return re.sub(
            r'https://(?:www\.)?(?:facebook|facebed)\.com(?![\w.@:-])/*',
            '/', super().format(record), flags=re.IGNORECASE,
        )


_console_handler = logging.StreamHandler()
_console_handler.setFormatter(ConsoleFormatter(
    '[%(levelname)s] [%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
))
logging.basicConfig(level=logging.INFO, handlers=[_console_handler])


def quote(s: str) -> str:
    return "".join([
        _quote_(char) if char in r"<>\"'#%{}[]|\\^~`" else char
        for char in s
    ])

def get_credit() -> str:
    return 'facebed by pi.kt'


class Utils:
    @staticmethod
    def normalize_facebook_path(url_or_path: str) -> str:
        value = str(url_or_path)
        parsed = urlparse(value)
        if not parsed.netloc and re.match(
            r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE
        ):
            parsed = urlparse(f'https://{value}')
        if not parsed.netloc:
            return value.lstrip('/')

        hostname = (parsed.hostname or '').lower()
        if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
            normalized = parsed.path.lstrip('/')
            if parsed.query:
                normalized += f'?{parsed.query}'
            return normalized
        return value

    @staticmethod
    def facebook_path_segments(url_or_path: str) -> tuple[str, ...]:
        value = str(url_or_path)
        parsed = urlparse(value)
        if parsed.netloc:
            hostname = (parsed.hostname or '').lower()
            if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
                return ()
            path = parsed.path
        else:
            normalized = Utils.normalize_facebook_path(value)
            parsed_normalized = urlparse('/' + normalized.lstrip('/'))
            path = parsed_normalized.path
        return tuple(part for part in path.split('/') if part)

    @staticmethod
    def is_facebook_url(url: str, *, require_https: bool = False) -> bool:
        parsed = urlparse(str(url))
        hostname = (parsed.hostname or '').lower()
        if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
            return False
        if require_https and parsed.scheme.lower() != 'https':
            return False
        return parsed.scheme.lower() in {'http', 'https'}

    @staticmethod
    def is_safe_media_url(url: str) -> bool:
        parsed = urlparse(str(url))
        return bool(
            parsed.scheme.lower() == 'https'
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )

    @staticmethod
    def is_control_path(url_or_path: str, *names: str) -> bool:
        parts = Utils.facebook_path_segments(url_or_path)
        if not parts:
            return False
        first = parts[0].casefold().removesuffix('.php')
        return first in {str(name).casefold().removesuffix('.php') for name in names}

    @staticmethod
    def is_post_like_path(url_or_path: str) -> bool:
        value = Utils.normalize_facebook_path(url_or_path)
        path = urlparse('/' + value.lstrip('/')).path
        return bool(
            re.search(
                r'/(?:posts|permalink|reel|watch|videos|v|photos)(?:/|$)',
                '/' + path.lstrip('/'),
                re.IGNORECASE,
            )
            or re.match(r'^/?(?:story|permalink|photo)\.php/?$', path, re.IGNORECASE)
        )

    @staticmethod
    def is_share_path(path: str) -> bool:
        return bool(re.match(r'^/?share(?:/|$)', urlparse(path).path, re.IGNORECASE))

    @staticmethod
    def is_group_landing_target(path: str) -> bool:
        normalized_path = urlparse(JsonParser.ensure_full_url(path)).path.strip('/')
        return normalized_path.startswith('groups/') and (
            normalized_path.endswith('/about')
            or len(normalized_path.split('/')) <= 2
        )

    @staticmethod
    def resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        with cffi.request_scope():
            try:
                resolved_path, prefetched_response = Utils._resolve_share_link(path)
                resolved_url = JsonParser.ensure_full_url(resolved_path)
                if not urlparse(resolved_url).path.strip('/'):
                    raise ShareResolutionException(
                        'Facebook share resolution did not produce a post target'
                    )
                return resolved_path, prefetched_response
            except ShareResolutionException:
                raise
            except UnsupportedRouteException:
                raise
            except NoDataException as exc:
                raise ShareResolutionException(
                    str(exc),
                    getattr(exc, 'upstream_response', None),
                    count_as_error=False,
                    reason_code=getattr(exc, 'reason_code', 'share_resolution_failed'),
                    terminal=getattr(exc, 'terminal', False),
                ) from exc

    @staticmethod
    def _resolve_share_link(path: str) -> tuple[str, CffiResponse | None]:
        source_path = Utils.normalize_facebook_path(path)
        url = JsonParser.ensure_full_url(source_path)
        source_parts = Utils.facebook_path_segments(source_path)
        is_share_v = (
            len(source_parts) >= 2
            and source_parts[0].casefold() == 'share'
            and source_parts[1].casefold() == 'v'
        )
        logging.info(f'resolving share link {url}')
        head_response = None
        try:
            head_response = cffi.head(
                url,
                headers={'User-Agent': SHARE_HEAD_USER_AGENT},
                _check_status=False,
            )
        except UpstreamException as exc:
            logging.warning('share HEAD failed for %s: %s', url, exc)

        needs_get = head_response is None
        if head_response is not None:
            head_url = str(head_response.url)
            head_path = Utils.normalize_facebook_path(head_url)
            parsed_head_url = urlparse(head_url)
            head_hostname = (parsed_head_url.hostname or '').lower()
            head_is_facebook = (
                head_hostname == 'facebook.com'
                or head_hostname.endswith('.facebook.com')
            )
            head_is_group_landing = Utils.is_group_landing_target(head_path)
            head_is_post_like = Utils.is_post_like_path(head_path)
            head_parts = Utils.facebook_path_segments(head_path)
            head_is_video_like = bool(
                head_parts
                and (
                    head_parts[0].casefold() in {'watch', 'reel'}
                    or any(part.casefold() == 'videos' for part in head_parts)
                    or (
                        head_parts[0].casefold() == 'groups'
                        and any(
                            part.casefold() in {'posts', 'permalink'}
                            for part in head_parts
                        )
                    )
                )
            )
            head_usable = (
                head_is_facebook
                and not head_is_group_landing
                and head_is_post_like
                and (not is_share_v or head_is_video_like)
            )
            needs_get = (
                head_response.status_code >= 400
                or Utils.is_share_path(head_path)
                or Utils.is_control_path(head_url, 'login', 'checkpoint', 'recover')
                or not head_usable
            )
            if not needs_get:
                logging.info(f'resolved to {head_response.url}')
                return head_path, None

        def inspect_direct(response: CffiResponse):
            response_url = str(response.url)
            parsed_response_url = urlparse(response_url)
            response_host = (parsed_response_url.hostname or '').lower()
            response_is_facebook = (
                response_host == 'facebook.com'
                or response_host.endswith('.facebook.com')
            )
            direct_url = response_url
            html_parser = BeautifulSoup(response.text, 'html.parser')
            canonical_candidates = []
            canonical_link = html_parser.select_one('link[rel="canonical"]')
            if canonical_link and canonical_link.get('href'):
                canonical_candidates.append(str(canonical_link['href']))
            for canonical_meta in html_parser.select('meta[property="og:url"]'):
                if canonical_meta and canonical_meta.get('content'):
                    canonical_candidates.append(str(canonical_meta['content']))
            declared_target = False
            for canonical_url in canonical_candidates:
                candidate_url = JsonParser.ensure_full_url(canonical_url)
                candidate_path = Utils.normalize_facebook_path(candidate_url)
                parsed_candidate = urlparse(candidate_url)
                candidate_host = (parsed_candidate.hostname or '').lower()
                candidate_is_facebook = (
                    candidate_host == 'facebook.com'
                    or candidate_host.endswith('.facebook.com')
                )
                candidate_path_only = parsed_candidate.path.strip('/')
                candidate_first_segment = candidate_path_only.partition('/')[0].lower()
                candidate_page = candidate_first_segment.removesuffix('.php')
                candidate_is_usable = (
                    bool(candidate_path_only)
                    and not Utils.is_share_path(candidate_path)
                    and candidate_page not in {'login', 'checkpoint', 'recover'}
                    and not Utils.is_group_landing_target(candidate_path)
                )
                if candidate_is_facebook and candidate_is_usable:
                    direct_url = candidate_url
                    declared_target = True
                    break
            direct_path = Utils.normalize_facebook_path(direct_url)
            page_type = 'unknown'
            if response_is_facebook:
                page_type = JsonParser.probe_page_type(
                    html_parser,
                    direct_path,
                    response_url=response_url,
                )
                if (
                    page_type == JsonParser.PROBE_UNKNOWN
                    and source_path != direct_path
                ):
                    page_type = JsonParser.probe_page_type(
                        html_parser,
                        source_path,
                        response_url=response_url,
                    )
            return (
                direct_url,
                direct_path,
                page_type,
                declared_target,
                response_is_facebook,
            )

        direct_response = cffi.get(
            url,
            headers={'User-Agent': SHARE_BODY_USER_AGENT},
            _check_status=False,
            _retry_status_responses=False,
        )
        (
            direct_url,
            direct_path,
            page_type,
            declared_target,
            response_is_facebook,
        ) = inspect_direct(direct_response)
        if (
            page_type != 'has_data'
            and direct_response.status_code in cffi.retry_statuses
        ):
            direct_response = cffi.get(
                url,
                headers={'User-Agent': SHARE_BODY_USER_AGENT},
                _check_status=False,
                _retry_status_responses=False,
                _bypass_cache=True,
            )
            (
                direct_url,
                direct_path,
                page_type,
                declared_target,
                response_is_facebook,
            ) = inspect_direct(direct_response)
        logging.info(f'resolved to {direct_url}')
        if not response_is_facebook:
            if declared_target:
                if Utils.is_group_landing_target(direct_path):
                    raise NoDataException(
                        'Facebook resolved share link to a group landing page'
                    )
                return direct_path, None
            raise NoDataException('Facebook redirected share link outside Facebook')
        if page_type in {
            JsonParser.PROBE_LOGIN_WALL,
            JsonParser.PROBE_PRIVATE_GROUP,
        }:
            JsonParser.raise_for_probe_result(page_type, direct_path)
        if direct_response.status_code >= 400 and page_type != 'has_data':
            raise UpstreamException(
                f'Facebook returned HTTP {direct_response.status_code} for {url}', direct_response
            )
        JsonParser.raise_for_probe_result(page_type, direct_path)
        if Utils.is_share_path(direct_path):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException(
                'Facebook left the share URL unresolved without post data',
                reason_code='share_target_unresolved',
            )
        if Utils.is_control_path(
            str(direct_response.url), 'login', 'checkpoint', 'recover'
        ):
            if page_type == 'has_data':
                return source_path, direct_response
            raise NoDataException(
                'Facebook redirected share link to authentication',
                reason_code='login_wall',
            )

        if Utils.is_group_landing_target(direct_path):
            raise NoDataException(
                'Facebook resolved share link to a group landing page',
                reason_code='private_group_post_unavailable',
            )
        if (
            page_type == JsonParser.PROBE_UNKNOWN
            and declared_target
            and direct_path != Utils.normalize_facebook_path(str(direct_response.url))
        ):
            # Metadata can identify a post without containing its payload.
            # Let the parser fetch that target instead of reusing the share page.
            return direct_path, None
        return (
            direct_path,
            direct_response
            if page_type in {
                JsonParser.PROBE_HAS_DATA,
                JsonParser.PROBE_UNKNOWN,
            }
            else None,
        )

    @staticmethod
    def prettify(txt: str) -> str:
        return indent(txt, indentation ='    ', newline = '\n', indent_text = True)

    @staticmethod
    def warn(msg: str = None, file_content: bytes = None, filename: str = None, embed: DiscordEmbed = None):
        def worker():
            wh = config.get('notifier_webhook', '')
            if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
                return
            try:
                webhook = DiscordWebhook(
                    url=wh,
                    content=msg,
                    rate_limit_retry=True,
                    timeout=30,
                )
                if embed:
                    webhook.add_embed(embed)
                if file_content is not None and filename:
                    webhook.add_file(file=file_content, filename=filename)
                webhook.execute()
            except Exception:
                logging.warning(f"couldn't warn about {msg or embed}")

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def timestamp_to_str(ts: int) -> str:
        if ts < 0:
            return ''
        dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=config['timezone'])))
        tztext = dt.strftime('%z')[:3]
        return '⌚ ' + dt.strftime('%Y/%m/%d %H:%M:%S ') + f'UTC{tztext}'

    @staticmethod
    def human_format(num):
        if type(num) == int or re.match('^[0-9]+$', str(num)):
            num = int(num)
            num = float('{:.3g}'.format(num))
            magnitude = 0
            while abs(num) >= 1000:
                magnitude += 1
                num /= 1000.0
            return '{}{}'.format('{:f}'.format(num).rstrip('0').rstrip('.'), ['', 'K', 'M', 'B', 'T'][magnitude])
        else:
            return str(num)

    @staticmethod
    def get_top_reaction_ids(feedback: dict) -> tuple[str, ...]:
        if not isinstance(feedback, dict):
            return ()
        top_reactions = feedback.get('top_reactions')
        if not isinstance(top_reactions, dict):
            return ()
        edges = top_reactions.get('edges')
        if not isinstance(edges, list):
            return ()

        ranked: list[tuple[float, int, str]] = []
        seen_ids: set[str] = set()
        for position, edge in enumerate(edges):
            if not isinstance(edge, dict):
                continue
            node = edge.get('node')
            if not isinstance(node, dict):
                continue

            reaction_id = str(node.get('id') or '')
            if reaction_id not in FACEBOOK_REACTION_EMOJIS:
                reaction_name = str(
                    node.get('localized_name') or node.get('name') or ''
                ).casefold()
                reaction_id = FACEBOOK_REACTION_NAME_IDS.get(reaction_name, '')
            if not reaction_id or reaction_id in seen_ids:
                continue

            count = edge.get('reaction_count')
            if count is None:
                count = edge.get('i18n_reaction_count')
            text = str(count or '').strip().upper().replace(',', '')
            multiplier = 1
            if text.endswith(('K', 'M', 'B')):
                multiplier = {
                    'K': 1_000,
                    'M': 1_000_000,
                    'B': 1_000_000_000,
                }[text[-1]]
                text = text[:-1]
            try:
                numeric_count = float(text) * multiplier
            except (TypeError, ValueError):
                continue
            if numeric_count <= 0:
                continue

            seen_ids.add(reaction_id)
            ranked.append((-numeric_count, position, reaction_id))

        ranked.sort()
        reaction_ids = tuple(item[2] for item in ranked[:2])
        return reaction_ids if len(reaction_ids) == 2 else ()

    @staticmethod
    def format_reactions_str(
        likes: str,
        cmts: str,
        shares: str,
        top_reaction_ids: tuple[str, ...] = (),
    ) -> str:
        emojis: list[str] = []
        for reaction_id in top_reaction_ids:
            emoji = FACEBOOK_REACTION_EMOJIS.get(reaction_id)
            if emoji and emoji not in emojis:
                emojis.append(emoji)
        reaction_prefix = ' '.join(emojis[:2]) if len(emojis) >= 2 else '❤️'
        likes_str = f'{reaction_prefix} {likes}' if likes != 'null' else ''
        cmts_str = f'💬 {cmts}' if cmts != 'null' else ''
        shares_str = f'🔁 {shares}' if shares != 'null' else ''
        fmt = ' • '.join([x for x in [likes_str, cmts_str, shares_str] if x]).replace(',', '.')
        return fmt


class Jq:
    @staticmethod
    def enumerate(obj: dict):
        result = []

        def collect(value):
            if isinstance(value, dict):
                result.append(value)
                for v in value.values():
                    if isinstance(v, list):
                        collect(v)
                for v in value.values():
                    if isinstance(v, dict):
                        collect(v)
                for v in value.values():
                    if not isinstance(v, (dict, list)):
                        collect(v)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        collect(item)
                for item in value:
                    if isinstance(item, list):
                        collect(item)
                for item in value:
                    if not isinstance(item, (dict, list)):
                        collect(item)

        collect(obj)
        return result

    @staticmethod
    def iterate(obj: dict, key: str, first: bool = False):
        result = []
        for oo in Jq.enumerate(obj):
            if key in oo:
                if first:
                    return oo[key]
                else:
                    result.append(oo[key])
        return result

    @staticmethod
    def all(obj: dict, key: str) -> list[dict]:
        return Jq.iterate(obj, key, first=False)

    @staticmethod
    def first(obj: dict, key: str) -> dict:
        return Jq.iterate(obj, key, first=True)

    @staticmethod
    def has(obj: dict, *args: str) -> bool:
        for k in args:
            found = False
            for oo in Jq.enumerate(obj):
                if k in oo:
                    found = True
                    break
            if not found:
                return False
        return True



class Story:
    author_name: str
    text: str
    image_links: list[str]
    video_links: list[str]
    url: str

    author_id: int
    attached_story: Self

    def __init__(self, story_json: dict):
        if 'actors' in story_json or ('node_v2' not in story_json and ('comet_sections' in story_json or 'creation_story' in story_json or 'feedback' in story_json or 'attachments' in story_json)):
            node_v2 = story_json
        else:
            node_v2 = Jq.first(story_json, 'node_v2')
        if not isinstance(node_v2, dict):
            node_v2 = {}

        self.author_name = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'name' in story_json['actors'][0]:
            self.author_name = story_json['actors'][0]['name']
        elif node_v2.get('actors') and isinstance(node_v2['actors'], list) and len(node_v2['actors']) > 0 and 'name' in node_v2['actors'][0]:
            self.author_name = node_v2['actors'][0]['name']
        elif node_v2.get('name') and isinstance(node_v2['name'], str) and len(node_v2['name']) > 2:
            self.author_name = node_v2['name']
        elif node_v2.get('short_name'):
            self.author_name = node_v2['short_name']
        elif story_json.get('name') and isinstance(story_json['name'], str) and len(story_json['name']) > 2:
            self.author_name = story_json['name']
        else:
            self.author_name = Jq.first(story_json, 'name') or Jq.first(story_json, 'localized_name') or ''

        self.text = ''
        if 'message' in story_json and story_json['message'] and 'text' in story_json['message']:
            self.text = story_json['message']['text']
        elif story_json.get('text') and isinstance(story_json['text'], str):
            self.text = story_json['text']
        else:
            self.text = Jq.first(story_json, 'text') or ''

        self.image_links = self.get_image_links_post_json(story_json)
        self.video_links = self.get_video_links(story_json)

        self.url = story_json.get('wwwURL') or node_v2.get('wwwURL') or story_json.get('url') or ''
        if not isinstance(self.url, str):
            self.url = ''

        self.author_id = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'id' in story_json['actors'][0]:
            self.author_id = story_json['actors'][0]['id']
        elif node_v2.get('actors') and isinstance(node_v2['actors'], list) and len(node_v2['actors']) > 0 and 'id' in node_v2['actors'][0]:
            self.author_id = node_v2['actors'][0]['id']
        elif node_v2.get('id'):
            self.author_id = node_v2['id']
        else:
            self.author_id = Jq.first(story_json, 'id') or ''

        if 'attached_story' in story_json and story_json['attached_story'] and 'actors' in story_json['attached_story']:
            self.attached_story = Story(story_json['attached_story'])
            self.image_links.extend([x for x in self.attached_story.image_links if x not in self.image_links])
            self.video_links.extend([x for x in self.attached_story.video_links if x not in self.video_links])
        else:
            self.attached_story = None

    # TODO: find better format for this
    def get_text(self) -> str:
        text = self.text
        if self.attached_story:
            text += f'\n╰┈➤ {self.attached_story.author_name}\n{self.attached_story.text}'
        return text

    @staticmethod
    def get_video_links(post_json: dict) -> list[str]:
        video_links = []
        for attachment_set in Jq.all(post_json, 'attachment'):
            try:
                link = ReelsParser.get_video_link(None, user_node=attachment_set)
                if link not in video_links:
                    video_links.append(link)
            except FacebedException:
                pass

        return video_links

    @staticmethod
    def get_image_links_post_json(post_json: dict) -> list[str]:
        def is_sticker(value: dict) -> bool:
            return any(
                isinstance(node, dict) and node.get('__typename') == 'Sticker'
                for node in [value, *Jq.enumerate(value)]
            )

        all_attachments = Jq.all(post_json, 'attachment')
        for attachment_set in all_attachments:
            if any([k.endswith('subattachments') for k in attachment_set]):
                subsets = [v for k, v in attachment_set.items() if k.endswith('subattachments') and 'nodes' in v]
                if subsets:
                    max_imgage_count = len(max(subsets, key=lambda it: len(it['nodes']))['nodes'])
                    subsets = [subset for subset in subsets if
                               len(subset['nodes']) == max_imgage_count and Jq.all(subset, 'viewer_image')]
                    if subsets:
                        images = [x['uri'] for x in Jq.all(subsets[0], 'viewer_image')]
                        if images:
                            return images
            elif 'media' in attachment_set and not is_sticker(attachment_set):
                simplet_set = [x['uri'] for x in Jq.all(attachment_set, 'photo_image')]
                if simplet_set:
                    return simplet_set
        one_img = Story.fallback_get_image_link(post_json)
        if one_img:
            return [one_img]
        return []

    # facebook broke the original selector for all single-image posts, circa 10/12/2024
    @staticmethod
    def fallback_get_image_link(post_json: dict) -> str:
        for aa in Jq.all(post_json, 'comet_photo_attachment_resolution_renderer'):
            return aa['image']['uri']
        return ''

@dataclass
class ParsedPost:
    author_name: str
    text: str
    image_links: list[str]
    url: str
    date: int

    likes: str
    comments: str
    shares: str
    video_links: list[str]
    top_reaction_ids: tuple[str, ...] = ()


def banned(url: str) -> ParsedPost:
    Utils.warn(f'banned embed attempted "{url}"')
    return ParsedPost('Banned', 'This user is banned by the operators of this embed server',
                      [], 'https://banned.facebook.com', -1,
                      'null', 'null', 'null', [])


class JsonParser:
    IDENTITY_FIELDS = frozenset({
        'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
        'storyfbid', 'story_id', 'storyid', 'fbid', 'legacy_fbid',
        'top_level_post_id', 'mf_story_key', 'feedback_id',
    })
    IDENTITY_URL_FIELDS = frozenset({
        'url', 'wwwurl', 'permalink', 'permalink_url', 'shareable_url',
        'canonical_url',
    })
    CONTENT_ROUTE_NAMES = frozenset({
        'comet.fbweb.CometSinglePostDialogRoute',
        'comet.fbweb.CometGroupLoggedOutPermalinkRoute',
        'comet.fbweb.CometFBReelsRoute',
        'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute',
        'comet.fbweb.CometPhotoRoute',
    })
    PROBE_HAS_DATA = 'has_data'
    PROBE_UNKNOWN = 'unknown'
    PROBE_NO_DATA = 'no_data'
    PROBE_LOGIN_WALL = 'login_wall'
    PROBE_TARGET_NOT_FOUND = 'target_not_found'
    PROBE_TARGET_IDENTITY_MISMATCH = 'target_identity_mismatch'
    PROBE_PRIVATE_GROUP = 'private_group_post_unavailable'
    PROBE_TARGET_VIDEO_MISSING = 'target_video_payload_missing'
    PROBE_UNSUPPORTED_PROFILE = 'unsupported_profile_target'
    PROBE_UNSUPPORTED_MARKETPLACE = 'unsupported_marketplace_target'
    PROBE_UNSUPPORTED_MEDIA_SET = 'unsupported_media_set_target'

    @staticmethod
    def get_headers() -> dict:
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'priority': 'u=0, i',
            'sec-ch-prefers-color-scheme': 'dark',
            'sec-ch-ua': '"Not)A;Brand";v="8", "Chromium";v="146"',
            'sec-ch-ua-full-version-list': '"Not)A;Brand";v="8.0.0.0", "Chromium";v="146.0.7680.80"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-model': '""',
            'sec-ch-ua-platform': '"Windows"',
            'sec-ch-ua-platform-version': '"19.0.0"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
            'sec-fetch-user': '?1',
            'sec-gpc': '1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
        }

        return headers

    @staticmethod
    def get_json_blocks(html_parser: BeautifulSoup) -> list[dict]:
        script_elements = html_parser.find_all('script', attrs={'type': 'application/json'})
        
        data_blocks = []
        for e in script_elements:
            try:
                if e.text:
                    data = json.loads(e.text)
                    data_blocks.append(data)
            except json.JSONDecodeError:
                continue
        return data_blocks

    @staticmethod
    def get_initial_routes(blocks: list[dict]) -> list[dict]:
        routes = []
        for block in blocks:
            route_info = Jq.first(block, 'initialRouteInfo')
            route = route_info.get('route') if isinstance(route_info, dict) else None
            if isinstance(route, dict) and route not in routes:
                routes.append(route)
        return routes

    @staticmethod
    def get_declared_urls(
        html_parser: BeautifulSoup,
        *,
        property_name: str | None = None,
    ) -> list[str]:
        urls: list[str] = []
        if property_name in (None, 'canonical'):
            for link in html_parser.find_all('link'):
                rel = link.get('rel') or []
                if 'canonical' in rel and link.get('href'):
                    urls.append(str(link['href']))
        if property_name in (None, 'og:url'):
            for meta in html_parser.find_all('meta', attrs={'property': 'og:url'}):
                if meta.get('content'):
                    urls.append(str(meta['content']))
        return list(dict.fromkeys(urls))

    @staticmethod
    def direct_identity_ids(node: dict) -> set[str]:
        identities: set[str] = set()
        if not isinstance(node, dict):
            return identities
        for key, item in node.items():
            key_name = str(key).lower()
            if key_name in JsonParser.IDENTITY_FIELDS:
                if isinstance(item, (str, int)) and str(item):
                    identities.add(str(item))
                elif isinstance(item, list):
                    identities.update(
                        str(part) for part in item
                        if isinstance(part, (str, int)) and str(part)
                    )
            elif key_name in JsonParser.IDENTITY_URL_FIELDS and isinstance(item, str):
                parsed_item = urlparse(item)
                if (
                    not parsed_item.netloc
                    or Utils.is_facebook_url(item, require_https=True)
                ):
                    identities.update(JsonParser.get_requested_ids(item))
        return identities

    @staticmethod
    def directly_contains_exact_id(node: dict, requested_ids: list[str]) -> bool:
        return bool(set(map(str, requested_ids)).intersection(
            JsonParser.direct_identity_ids(node)
        ))

    @staticmethod
    def direct_video_identity_ids(node: dict) -> set[str]:
        if not isinstance(node, dict):
            return set()
        identities = {
            str(node[key])
            for key in ('id', 'video_id', 'videoid')
            if isinstance(node.get(key), (str, int)) and str(node[key])
        }
        legacy = node.get('videoDeliveryLegacyFields')
        if isinstance(legacy, dict):
            identities.update(
                str(legacy[key])
                for key in ('id', 'video_id', 'videoid')
                if isinstance(legacy.get(key), (str, int)) and str(legacy[key])
            )
        return identities

    @staticmethod
    def direct_video_url(node: dict) -> str | None:
        if not isinstance(node, dict):
            return None
        for key in ('browser_native_hd_url', 'browser_native_sd_url'):
            value = node.get(key)
            if isinstance(value, str) and Utils.is_safe_media_url(value):
                return value
        legacy = node.get('videoDeliveryLegacyFields')
        if isinstance(legacy, dict):
            for key in ('browser_native_hd_url', 'browser_native_sd_url'):
                value = legacy.get(key)
                if isinstance(value, str) and Utils.is_safe_media_url(value):
                    return value
        return None

    @staticmethod
    def get_video_delivery_nodes(blocks: list[dict]) -> list[dict]:
        nodes: list[dict] = []
        seen: set[int] = set()
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if not isinstance(node, dict) or not JsonParser.direct_video_url(node):
                    continue
                marker = id(node)
                if marker not in seen:
                    seen.add(marker)
                    nodes.append(node)
        return nodes

    @staticmethod
    def has_target_video_delivery(blocks: list[dict], target_ids: list[str]) -> bool:
        target_id_set = set(map(str, target_ids))
        return any(
            target_id_set.intersection(
                JsonParser.direct_video_identity_ids(node)
            )
            for node in JsonParser.get_video_delivery_nodes(blocks)
        )

    @staticmethod
    def has_bounded_target_payload(
        blocks: list[dict], target_ids: list[str]
    ) -> bool:
        if not target_ids:
            return False
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if not isinstance(node, dict):
                    continue
                if not JsonParser.directly_contains_exact_id(node, target_ids):
                    continue
                if JsonParser.direct_video_url(node):
                    return True
                if isinstance(node.get('creation_story'), dict):
                    return True
                for key in (
                    'actors', 'attachments', 'comet_sections', 'content',
                    'message', 'owner', 'title', 'image', 'photo_image',
                    'container_story', 'attached_comment',
                ):
                    value = node.get(key)
                    if value not in (None, '', [], {}):
                        return True
        return False

    @staticmethod
    def get_terminal_video_id(post_path: str) -> str | None:
        parsed = urlparse(JsonParser.ensure_full_url(post_path))
        parts = [part for part in parsed.path.split('/') if part]
        videos_index = next(
            (
                index for index, part in enumerate(parts)
                if part.casefold() == 'videos'
            ),
            None,
        )
        if videos_index is None:
            return None
        video_parts = parts[videos_index + 1:]
        if (
            len(video_parts) not in {1, 2}
            or not video_parts[-1].isdigit()
        ):
            return None
        return video_parts[-1]

    @staticmethod
    def get_requested_ids(post_path: str) -> list[str]:
        parsed = urlparse(JsonParser.ensure_full_url(post_path))
        requested_ids = []
        query = parse_qs(parsed.query)
        for key in ('story_fbid', 'fbid', 'v', 'multi_permalinks', 'post_id'):
            requested_ids.extend(query.get(key, []))

        posts_match = re.search(r'/posts/(.+)', parsed.path, re.IGNORECASE)
        if posts_match:
            post_parts = [part for part in posts_match.group(1).split('/') if part]
            post_id = next(
                (part for part in post_parts if part.lower().startswith('pfbid')),
                None,
            )
            if post_id is None:
                post_id = next(
                    (part for part in reversed(post_parts) if part.isdigit()),
                    post_parts[0] if post_parts else None,
                )
            if post_id:
                requested_ids.append(post_id)

        video_id = JsonParser.get_terminal_video_id(post_path)
        if video_id:
            requested_ids.append(video_id)

        for pattern in (
            r'/reel/([^/?]+)',
            r'/watch/([^/?]+)',
            r'(?<!share)/v/(\d+)(?:/|$)',
            r'/permalink/([^/?]+)',
            r'/photos/(?:[^/]+/)?([^/?]+)/?$',
        ):
            match = re.search(pattern, parsed.path, re.IGNORECASE)
            if match:
                requested_ids.append(match.group(1))
        return list(dict.fromkeys(value for value in requested_ids if value))

    @staticmethod
    def get_target_ids(
        html_parser: BeautifulSoup,
        post_path: str,
        response_url: str | None = None,
    ) -> tuple[list[str], list[str]]:
        raw_ids = JsonParser.get_requested_ids(post_path)
        route_proven_ids: list[str] = []
        used_post_to_video_alias = False
        source_is_share = Utils.is_share_path(post_path)
        blocks = JsonParser.get_json_blocks(html_parser)

        selected_response = cffi.selected_get_response
        if response_url is None and selected_response is not None:
            response_url = str(getattr(selected_response, 'url', '') or '')
        response_url = str(response_url or '')
        response_ids = JsonParser.get_requested_ids(response_url)
        response_history = (
            getattr(selected_response, 'history', None)
            if selected_response is not None else None
        )
        response_was_redirected = bool(response_history)

        canonical_urls = JsonParser.get_declared_urls(
            html_parser, property_name='canonical'
        )
        og_urls = JsonParser.get_declared_urls(
            html_parser, property_name='og:url'
        )

        def add(value, target: list[str]) -> None:
            if isinstance(value, (str, int)) and str(value) and str(value) not in target:
                target.append(str(value))

        def share_key(value: str) -> str:
            normalized = Utils.normalize_facebook_path(value)
            return urlparse(JsonParser.ensure_full_url(normalized)).path.rstrip('/')

        source_share_key = share_key(post_path) if source_is_share else ''

        def media_links_source_to_target(candidate_id: str) -> bool:
            for block in blocks:
                for media in Jq.all(block, 'media'):
                    if not isinstance(media, dict):
                        continue
                    if candidate_id not in JsonParser.direct_video_identity_ids(
                        media
                    ):
                        continue
                    creation_story = media.get('creation_story')
                    if not isinstance(creation_story, dict):
                        continue
                    comet_sections = creation_story.get('comet_sections')
                    title = (
                        comet_sections.get('title')
                        if isinstance(comet_sections, dict) else None
                    )
                    story = title.get('story') if isinstance(title, dict) else None
                    post_id = story.get('post_id') if isinstance(story, dict) else None
                    if isinstance(post_id, (str, int)) and str(post_id) in raw_ids:
                        return True
            return False

        def declared_url_consensus(urls: list[str], target_id: str) -> bool:
            return bool(urls) and all(
                Utils.is_facebook_url(url, require_https=True)
                and set(JsonParser.get_requested_ids(url)) == {target_id}
                for url in urls
            )

        def redirect_provenance_matches_source() -> bool:
            if (
                selected_response is None
                or not response_history
                or response_url
                != str(getattr(selected_response, 'url', '') or '')
            ):
                return False
            first_hop_url = str(
                getattr(response_history[0], 'url', '') or ''
            )
            return bool(
                Utils.is_facebook_url(first_hop_url, require_https=True)
                and '/posts/' in urlparse(first_hop_url).path.lower()
                and set(raw_ids).intersection(
                    JsonParser.get_requested_ids(first_hop_url)
                )
            )

        for route in JsonParser.get_initial_routes(blocks):
            route_name = str(route.get('canonicalRouteName', ''))
            route_url = str(route.get('url', ''))
            if (
                route_name not in JsonParser.CONTENT_ROUTE_NAMES
                and not Utils.is_post_like_path(route_url)
            ):
                continue
            route_ids = JsonParser.get_requested_ids(route_url)

            candidate_ids: list[str] = []
            for value in route_ids:
                add(value, candidate_ids)

            params = route.get('params')
            route_share_keys: list[str] = []
            route_param_ids: set[str] = set()
            if isinstance(params, dict):
                for key in ('story_token', 'story_fbid', 'fbid', 'video_id', 'v', 'post_id'):
                    add(params.get(key), candidate_ids)
                    if params.get(key) not in (None, ''):
                        route_param_ids.add(str(params[key]))
                if params.get('share_url'):
                    route_share_keys.append(share_key(str(params['share_url'])))

            route_url_query = parse_qs(urlparse(str(route.get('url', ''))).query)
            for share_url in route_url_query.get('share_url', []):
                route_share_keys.append(share_key(str(share_url)))

            for view_key in ('rootView', 'hostableView'):
                view = route.get(view_key)
                props = view.get('props') if isinstance(view, dict) else None
                if not isinstance(props, dict):
                    continue
                for key in ('storyID', 'videoID', 'postID'):
                    add(props.get(key), candidate_ids)

            route_matches_request = bool(
                raw_ids and set(raw_ids).intersection(candidate_ids)
            )
            opaque_share_target = bool(
                source_is_share
                and not raw_ids
                and candidate_ids
                and source_share_key in route_share_keys
            )
            final_id_set = {
                value for value in response_ids
                if value.isdigit()
            }
            final_video_id = (
                next(iter(final_id_set))
                if len(final_id_set) == 1 else None
            )
            declared_final_target = bool(
                final_video_id
                and set(route_ids) == {final_video_id}
                and route_param_ids
                and route_param_ids == {final_video_id}
                and declared_url_consensus(canonical_urls, final_video_id)
                and declared_url_consensus(og_urls, final_video_id)
            )
            direct_loe_target = (
                next(iter(set(route_ids)))
                if (
                    route_name
                    == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
                    and len(set(route_ids)) == 1
                    and route_param_ids == set(route_ids)
                )
                else None
            )
            direct_loe_consensus = bool(
                direct_loe_target
                and set(raw_ids) == {direct_loe_target}
                and (
                    not response_url
                    or Utils.is_facebook_url(
                        response_url, require_https=True
                    )
                )
                and (
                    not response_ids
                    or set(response_ids) == {direct_loe_target}
                )
                and declared_url_consensus(
                    canonical_urls, str(direct_loe_target)
                )
                and declared_url_consensus(og_urls, str(direct_loe_target))
                and JsonParser.has_target_video_delivery(
                    blocks, [str(direct_loe_target)]
                )
            )
            if (
                route_name
                == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
                and route_matches_request
                and not direct_loe_consensus
            ):
                route_matches_request = False
            raw_ids_are_pfbids = bool(raw_ids) and all(
                value.lower().startswith('pfbid') for value in raw_ids
            )
            post_to_video_alias = bool(
                not source_is_share
                and raw_ids
                and '/posts/' in urlparse(
                    JsonParser.ensure_full_url(post_path)
                ).path.lower()
                and response_was_redirected
                and redirect_provenance_matches_source()
                and Utils.is_facebook_url(response_url, require_https=True)
                and '/videos/' in urlparse(response_url).path.lower()
                and route_name
                == 'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute'
                and declared_final_target
                and JsonParser.has_target_video_delivery(
                    blocks, [str(final_video_id)]
                )
                and (
                    raw_ids_are_pfbids
                    or media_links_source_to_target(str(final_video_id))
                )
            )
            if not (
                route_matches_request
                or opaque_share_target
                or post_to_video_alias
            ):
                continue
            if post_to_video_alias:
                used_post_to_video_alias = True
                add(final_video_id, route_proven_ids)
            elif direct_loe_consensus:
                add(direct_loe_target, route_proven_ids)
            else:
                for value in candidate_ids:
                    add(value, route_proven_ids)

        selection_ids = (
            route_proven_ids
            if used_post_to_video_alias
            else list(dict.fromkeys([*route_proven_ids, *raw_ids]))
        )
        return (
            selection_ids,
            route_proven_ids,
        )

    @staticmethod
    def contains_exact_id(value, requested_ids: list[str]) -> bool:
        if not requested_ids:
            return False
        if isinstance(value, dict):
            id_fields = {
                'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
                'storyfbid', 'story_id', 'storyid', 'fbid', 'legacy_fbid', 'top_level_post_id',
                'mf_story_key', 'feedback_id',
            }
            for key, item in value.items():
                if str(key).lower() in id_fields and isinstance(item, (str, int)):
                    if str(item) in requested_ids:
                        return True
                if str(key).lower() in id_fields and isinstance(item, list):
                    if any(
                        isinstance(part, (str, int)) and str(part) in requested_ids
                        for part in item
                    ):
                        return True
                if JsonParser.contains_exact_id(item, requested_ids):
                    return True
        elif isinstance(value, list):
            return any(JsonParser.contains_exact_id(item, requested_ids) for item in value)
        return False

    @staticmethod
    def contains_target_id(value, requested_ids: list[str]) -> bool:
        if not requested_ids:
            return False
        if isinstance(value, dict):
            identity_urls = {
                'url', 'wwwurl', 'permalink', 'permalink_url',
                'shareable_url', 'canonical_url',
            }
            for key, item in value.items():
                if str(key).lower() in identity_urls and isinstance(item, str):
                    if set(JsonParser.get_requested_ids(item)).intersection(requested_ids):
                        return True
                if JsonParser.contains_target_id(item, requested_ids):
                    return True
            return JsonParser.contains_exact_id(value, requested_ids)
        if isinstance(value, list):
            return any(JsonParser.contains_target_id(item, requested_ids) for item in value)
        return False

    @staticmethod
    def select_requested_candidate(candidates, requested_ids: list[str]):
        candidates = [candidate for candidate in candidates if isinstance(candidate, dict)]
        for candidate in candidates:
            if JsonParser.contains_exact_id(candidate, requested_ids):
                return candidate
        for candidate in candidates:
            if JsonParser.contains_target_id(candidate, requested_ids):
                return candidate
        if requested_ids:
            return None
        return candidates[0] if candidates else None

    @staticmethod
    def select_requested_field(blocks: list[dict], field_name: str, requested_ids: list[str]):
        matching = []
        for block in blocks:
            for node in [block, *Jq.enumerate(block)]:
                if field_name not in node:
                    continue
                if JsonParser.contains_exact_id(node, requested_ids):
                    matching.append((node, node[field_name]))
                elif JsonParser.contains_target_id(node, requested_ids):
                    matching.append((node, node[field_name]))
        if matching:
            return min(matching, key=lambda item: len(str(item[0])))[1]
        if requested_ids:
            return None
        for block in blocks:
            value = Jq.first(block, field_name)
            if value not in (None, [], ''):
                return value
        return None

    @staticmethod
    def probe_page_type(
        html_parser: BeautifulSoup,
        post_path: str = '',
        response_url: str | None = None,
    ) -> str:
        blocks = JsonParser.get_json_blocks(html_parser)
        routes = JsonParser.get_initial_routes(blocks)
        route_names = {
            str(route.get('canonicalRouteName', '')) for route in routes
        }
        if response_url is None and cffi.selected_get_response is not None:
            response_url = str(
                getattr(cffi.selected_get_response, 'url', '') or ''
            )
        response_url = str(response_url or '')
        raw_ids = JsonParser.get_requested_ids(post_path)
        target_ids, route_ids = JsonParser.get_target_ids(
            html_parser, post_path, response_url=response_url
        )
        required_ids = route_ids or raw_ids or target_ids
        canonical_urls = JsonParser.get_declared_urls(
            html_parser, property_name='canonical'
        )
        og_urls = JsonParser.get_declared_urls(
            html_parser, property_name='og:url'
        )

        unsupported_routes = {
            'comet.fbweb.CometProfilePlusLoggedOutRoute':
                JsonParser.PROBE_UNSUPPORTED_PROFILE,
            'comet.fbweb.CometProfileLoggedOutRoute':
                JsonParser.PROBE_UNSUPPORTED_PROFILE,
            'comet.fbweb.CometMarketplaceHoistedPermalinkRoute':
                JsonParser.PROBE_UNSUPPORTED_MARKETPLACE,
            'comet.fbweb.CometProfileLegacyAlbumViewRoute':
                JsonParser.PROBE_UNSUPPORTED_MEDIA_SET,
        }
        for route_name, result in unsupported_routes.items():
            if route_name in route_names:
                return result

        def has_private_group_surface() -> bool:
            for block in blocks:
                for group in Jq.all(block, 'group'):
                    if not isinstance(group, dict):
                        continue
                    join_states = {
                        str(group.get('viewer_join_state', '')),
                        str(group.get('viewer_forum_join_state', '')),
                    }
                    privacy = group.get('privacy_info')
                    title = Jq.first(privacy, 'text') if isinstance(privacy, dict) else None
                    if (
                        join_states.intersection({
                            'CANNOT_JOIN', 'CANNOT_JOIN_OR_REQUEST'
                        })
                        and title == 'Private group'
                    ):
                        return True
            return False

        def reel_content_state(reel_id: str) -> tuple[bool, bool]:
            has_context = False
            has_missing_stub = False
            for block in blocks:
                for story in Jq.all(block, 'creation_story'):
                    if (
                        isinstance(story, dict)
                        and JsonParser.contains_exact_id(story, [reel_id])
                    ):
                        has_context = True
                for video in Jq.all(block, 'video'):
                    if (
                        isinstance(video, dict)
                        and reel_id
                        in JsonParser.direct_video_identity_ids(video)
                        and 'creation_story' in video
                        and video.get('creation_story') is None
                    ):
                        has_missing_stub = True
                for context in Jq.all(block, 'short_form_video_context'):
                    if not isinstance(context, dict):
                        continue
                    for key in ('video', 'playback_video'):
                        video = context.get(key)
                        if (
                            isinstance(video, dict)
                            and reel_id
                            in JsonParser.direct_video_identity_ids(video)
                        ):
                            has_context = True
                    shareable_url = context.get('shareable_url')
                    if (
                        isinstance(shareable_url, str)
                        and reel_id
                        in JsonParser.get_requested_ids(shareable_url)
                    ):
                        has_context = True
            return has_context, has_missing_stub

        def reel_consensus_id() -> str | None:
            reel_routes = [
                route for route in routes
                if route.get('canonicalRouteName')
                == 'comet.fbweb.CometFBReelsRoute'
            ]
            if len(reel_routes) != 1:
                return None
            route = reel_routes[0]
            route_ids = set(JsonParser.get_requested_ids(str(route.get('url', ''))))
            params = route.get('params')
            param_id = (
                str(params.get('video_id'))
                if isinstance(params, dict) and params.get('video_id') else ''
            )
            if len(route_ids) != 1 or not param_id or route_ids != {param_id}:
                return None
            requested_id_set = set(raw_ids)
            selected_id_set = set(JsonParser.get_requested_ids(response_url))
            if (
                (requested_id_set and requested_id_set != {param_id})
                or (selected_id_set and selected_id_set != {param_id})
                or not (requested_id_set or selected_id_set)
            ):
                return None
            if not canonical_urls or not og_urls:
                return None
            for urls in (canonical_urls, og_urls):
                if not all(
                    Utils.is_facebook_url(url, require_https=True)
                    and set(JsonParser.get_requested_ids(url)) == {param_id}
                    for url in urls
                ):
                    return None
            return param_id

        declared_urls = [*canonical_urls, *og_urls]
        auth_wall = bool(
            _authentication_wall_reason(response_url, html_parser)
            or (
                declared_urls
                and all(
                    Utils.is_control_path(
                        url, 'login', 'checkpoint', 'recover'
                    )
                    for url in declared_urls
                )
            )
        )

        if 'comet.fbweb.CometVideoHomeVideoNotFoundRoute' in route_names:
            return JsonParser.PROBE_TARGET_NOT_FOUND

        if 'comet.fbweb.CometGroupAboutRoute' in route_names:
            canonical_about = any(
                len(Utils.facebook_path_segments(url)) >= 3
                and Utils.facebook_path_segments(url)[0].casefold() == 'groups'
                and Utils.facebook_path_segments(url)[-1].casefold() == 'about'
                for url in canonical_urls
            )
            if (
                any(
                    Utils.is_post_like_path(path)
                    for path in (post_path, response_url) if path
                )
                and canonical_about
                and has_private_group_surface()
                and not JsonParser.has_bounded_target_payload(
                    blocks, required_ids
                )
            ):
                return JsonParser.PROBE_PRIVATE_GROUP

        if (
            'comet.fbweb.CometVideoHomeLOEVideoPermalinkRoute' in route_names
            and not route_ids
            and (
                raw_ids
                or Utils.is_post_like_path(post_path)
            )
        ):
            return JsonParser.PROBE_TARGET_IDENTITY_MISMATCH

        if 'comet.fbweb.CometFBReelsRoute' in route_names:
            reel_id = reel_consensus_id()
            has_context, has_missing_stub = (
                reel_content_state(reel_id) if reel_id else (False, False)
            )
            has_delivery = bool(
                reel_id
                and JsonParser.has_target_video_delivery(blocks, [reel_id])
            )
            if has_context and has_delivery:
                return JsonParser.PROBE_HAS_DATA
            if auth_wall:
                return JsonParser.PROBE_LOGIN_WALL
            if has_missing_stub and not has_context and not has_delivery:
                return JsonParser.PROBE_TARGET_VIDEO_MISSING
            if (
                not reel_id
                and Utils.is_post_like_path(post_path)
                and (
                    route_ids
                    or raw_ids
                    or JsonParser.get_requested_ids(response_url)
                )
            ):
                return JsonParser.PROBE_TARGET_IDENTITY_MISMATCH

        if JsonParser.has_bounded_target_payload(blocks, required_ids):
            return JsonParser.PROBE_HAS_DATA
        if auth_wall:
            return JsonParser.PROBE_LOGIN_WALL
        return JsonParser.PROBE_UNKNOWN

    @staticmethod
    def raise_for_probe_result(page_type: str, post_path: str) -> None:
        unsupported_messages = {
            JsonParser.PROBE_UNSUPPORTED_PROFILE:
                'Facebook share target is a profile rather than a post',
            JsonParser.PROBE_UNSUPPORTED_MARKETPLACE:
                'Facebook share target is a Marketplace listing',
            JsonParser.PROBE_UNSUPPORTED_MEDIA_SET:
                'Facebook share target is a media-set album',
        }
        if page_type in unsupported_messages:
            raise UnsupportedRouteException(
                unsupported_messages[page_type], reason_code=page_type
            )
        messages = {
            JsonParser.PROBE_LOGIN_WALL:
                f'Facebook served a login wall for {post_path}',
            JsonParser.PROBE_PRIVATE_GROUP:
                f'Facebook served a private group landing without the requested post for {post_path}',
            JsonParser.PROBE_TARGET_VIDEO_MISSING:
                f'Facebook response has no target-local playable video for {post_path}',
            JsonParser.PROBE_TARGET_NOT_FOUND:
                f'Facebook reported that the requested target was not found for {post_path}',
            JsonParser.PROBE_TARGET_IDENTITY_MISMATCH:
                f'Facebook route identities disagree for {post_path}',
            JsonParser.PROBE_NO_DATA:
                f'Facebook response did not contain target data for {post_path}',
        }
        if page_type in messages:
            raise NoDataException(
                messages[page_type], reason_code=page_type, terminal=True
            )

    @staticmethod
    def check_page_or_raise(
        html_parser: BeautifulSoup,
        post_path: str,
        response_url: str | None = None,
    ) -> str:
        page_type = JsonParser.probe_page_type(
            html_parser, post_path, response_url=response_url
        )
        JsonParser.raise_for_probe_result(page_type, post_path)
        return page_type

    @staticmethod
    @contextmanager
    def fetch_page(
        post_path: str,
        http_response: CffiResponse | None = None,
    ):
        url = JsonParser.ensure_full_url(post_path)
        owns_response = http_response is None
        if owns_response:
            http_response = cffi.get(
                url,
                _check_status=False,
                _retry_status_responses=False,
            )

        def prepare_page(current_response: CffiResponse):
            cffi.select_response(current_response)
            current_url = str(getattr(current_response, 'url', '') or '')
            if current_url and not Utils.is_facebook_url(
                current_url, require_https=True
            ):
                raise UnsupportedRouteException(
                    'Facebook redirected outside Facebook',
                    reason_code='offsite_redirect',
                )
            current_html = current_response.text
            current_parser = BeautifulSoup(current_html, 'html.parser')
            page_type = JsonParser.probe_page_type(
                current_parser,
                post_path,
                response_url=current_url,
            )
            return current_html, current_parser, page_type

        raw_html, html_parser, page_type = prepare_page(http_response)
        if (
            owns_response
            and http_response.status_code in cffi.retry_statuses
            and page_type != JsonParser.PROBE_HAS_DATA
        ):
            http_response = cffi.get(
                url,
                _check_status=False,
                _retry_status_responses=False,
                _bypass_cache=True,
            )
            raw_html, html_parser, page_type = prepare_page(http_response)
        if page_type in {
            JsonParser.PROBE_LOGIN_WALL,
            JsonParser.PROBE_PRIVATE_GROUP,
        }:
            JsonParser.raise_for_probe_result(page_type, post_path)
        if (
            http_response.status_code >= 400
            and page_type != JsonParser.PROBE_HAS_DATA
        ):
            raise UpstreamException(
                f'Facebook returned HTTP {http_response.status_code} for {url}',
                http_response,
            )
        JsonParser.raise_for_probe_result(page_type, post_path)
        try:
            yield html_parser
        except ParseException as e:
            if not e.html:
                e.html = raw_html
                e.url = url
            raise

    @staticmethod
    def get_post_json(
        html_parser: BeautifulSoup,
        post_path: str = '',
        requested_ids: list[str] | None = None,
        required_ids: list[str] | None = None,
    ) -> dict:
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = (
            JsonParser.get_requested_ids(post_path)
            if requested_ids is None else requested_ids
        )
        required_ids = required_ids or []
        candidate_blocks = []
        for bloc in blocks:
            try:
                root = JsonParser.get_root_node(bloc, requested_ids)
            except ParseException:
                continue
            if required_ids and not JsonParser.contains_target_id(root, required_ids):
                continue
            candidate_blocks.append(bloc)

        if not candidate_blocks:
            if required_ids:
                if JsonParser.has_bounded_target_payload(
                    blocks, required_ids
                ):
                    raise ParseException(
                        'Facebook target payload shape is unsupported'
                    )
                raise NoDataException(
                    'Facebook response did not contain the requested post',
                    reason_code='target_payload_missing',
                )
            raise ParseException('cannot find post json')

        def score_block(bloc: dict) -> int:
            score = 0
            if Jq.has(bloc, 'short_form_video_context'):
                score += 20
            if Jq.has(bloc, 'creation_story'):
                score += 10
            if Jq.has(bloc, 'comet_sections'):
                score += 5
            if Jq.has(bloc, 'group_hoisted_feed'):
                score += 8
            if Jq.has(bloc, 'video_home_www_related_videos_section') or Jq.has(bloc, 'video_home_www_loe_video_permalink_seo_info'):
                score -= 20
            node_v2 = Jq.first(bloc, 'node_v2')
            if isinstance(node_v2, dict):
                if 'actors' in node_v2 and node_v2['actors']:
                    score += 30
                if 'feedback' in node_v2 and isinstance(node_v2['feedback'], dict):
                    score += 15
                if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                    score += 10
            data_blob = Jq.first(bloc, 'data')
            if isinstance(data_blob, dict):
                if 'actors' in data_blob and data_blob['actors']:
                    score += 20
                if 'feedback' in data_blob and isinstance(data_blob['feedback'], dict):
                    score += 10
            if 'require' in bloc and not node_v2 and not data_blob:
                score -= 10
            return score

        candidate_blocks.sort(
            key=lambda block: (
                JsonParser.contains_target_id(block, required_ids),
                JsonParser.contains_exact_id(block, requested_ids),
                JsonParser.contains_target_id(block, requested_ids),
                score_block(block),
            ),
            reverse=True,
        )
        for candidate in candidate_blocks:
            try:
                root = JsonParser.get_root_node(candidate, requested_ids)
                if required_ids and not JsonParser.contains_target_id(root, required_ids):
                    continue
                return candidate
            except ParseException:
                continue
        if required_ids:
            if JsonParser.has_bounded_target_payload(blocks, required_ids):
                raise ParseException(
                    'Facebook target payload shape is unsupported'
                )
            raise NoDataException(
                'Facebook response did not contain the requested post',
                reason_code='target_payload_missing',
            )
        return candidate_blocks[0]

    @staticmethod
    def get_group_name(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> str:
        candidates = [
            bloc for bloc in JsonParser.get_json_blocks(html_parser)
            if Jq.has(bloc, 'group_member_profiles', 'formatted_count_text')
        ]
        requested_ids = requested_ids or []
        selected = JsonParser.select_requested_candidate(candidates, requested_ids)
        if selected is None:
            return ''
        for group_object in Jq.all(selected, 'group'):
            if isinstance(group_object, dict) and 'name' in group_object:
                return group_object['name']
        return ''

    @staticmethod
    def get_interaction_counts(
        post_json: dict,
        requested_ids: list[str] | None = None,
        top_reaction_ids: list[str] | None = None,
    ) -> tuple[str, str, str]:
        assert post_json
        if top_reaction_ids is not None:
            top_reaction_ids.clear()

        def extract_counts(fb: dict) -> tuple[str, str, str]:
            if top_reaction_ids is not None:
                top_reaction_ids.extend(Utils.get_top_reaction_ids(fb))
            reaction_count = fb.get('reaction_count')
            if isinstance(reaction_count, dict):
                reaction_count = reaction_count.get('count')
            share_count = fb.get('share_count')
            if isinstance(share_count, dict):
                share_count = share_count.get('count')
            reactions = fb.get('i18n_reaction_count') or reaction_count or Jq.first(fb, 'i18n_reaction_count') or '0'
            shares = fb.get('i18n_share_count') or share_count or Jq.first(fb, 'i18n_share_count') or Jq.first(fb, 'share_count') or '0'
            comments = fb.get('total_comment_count') or Jq.first(fb, 'total_comment_count')
            if not comments:
                cri = fb.get('comment_rendering_instance')
                if isinstance(cri, dict):
                    cnode = cri.get('comments')
                    if isinstance(cnode, dict):
                        comments = cnode.get('total_count')
                if not comments:
                    ccsr = fb.get('comments_count_summary_renderer')
                    if isinstance(ccsr, dict):
                        fb_inner = ccsr.get('feedback')
                        if isinstance(fb_inner, dict):
                            cri2 = fb_inner.get('comment_rendering_instance')
                            if isinstance(cri2, dict):
                                cnode2 = cri2.get('comments')
                                if isinstance(cnode2, dict):
                                    comments = cnode2.get('total_count')
            if not comments:
                comments = '0'
            return str(reactions), str(comments), str(shares)

        def best_feedback() -> dict | None:
            best = None
            best_reactions = 0
            for fb in Jq.all(post_json, 'feedback'):
                if isinstance(fb, dict):
                    rc = fb.get('i18n_reaction_count')
                    if rc:
                        try:
                            n = int(rc)
                            if n > best_reactions:
                                best_reactions = n
                                best = fb
                        except (ValueError, TypeError):
                            pass
                    elif best is None:
                        best = fb
            return best

        requested_ids = [str(value) for value in (requested_ids or []) if value]
        id_keys = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
            'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
            'top_level_post_id', 'mf_story_key', 'feedback_id',
        }
        direct_feedback = post_json.get('feedback')
        for identity_source in (post_json, direct_feedback):
            if not isinstance(identity_source, dict):
                continue
            for key, item in identity_source.items():
                if str(key).lower() not in id_keys:
                    continue
                if isinstance(item, (str, int)) and str(item):
                    requested_ids.append(str(item))
                elif isinstance(item, list):
                    requested_ids.extend(
                        str(part) for part in item
                        if isinstance(part, (str, int)) and str(part)
                    )
        requested_ids = list(dict.fromkeys(requested_ids))
        if requested_ids:
            contextual_feedbacks: list[dict] = []

            def walk(value, inherited_ids: set[str] | None = None) -> None:
                inherited_ids = inherited_ids or set()
                if isinstance(value, dict):
                    own_ids: set[str] = set()
                    for key, item in value.items():
                        if (
                            str(key).lower() not in id_keys
                            or not isinstance(item, (str, int))
                        ):
                            continue
                        text = str(item)
                        own_ids.add(text)
                        try:
                            decoded = base64.b64decode(
                                text + ('=' * (-len(text) % 4)),
                                validate=True,
                            ).decode('utf-8')
                        except (ValueError, UnicodeDecodeError):
                            continue
                        for requested_id in requested_ids:
                            if requested_id == decoded or (
                                requested_id.isdigit()
                                and re.search(
                                    rf'(?<![A-Za-z0-9]){re.escape(requested_id)}(?![A-Za-z0-9])',
                                    decoded,
                                )
                            ):
                                own_ids.add(requested_id)
                    context_ids = own_ids or inherited_ids
                    feedback = value.get('feedback')
                    if (
                        isinstance(feedback, dict)
                        and context_ids.intersection(requested_ids)
                    ):
                        contextual_feedbacks.append(feedback)
                    for item in value.values():
                        walk(item, context_ids)
                elif isinstance(value, list):
                    for item in value:
                        walk(item, inherited_ids)

            walk(post_json)
            if isinstance(direct_feedback, dict) and any(
                marker in direct_feedback
                for marker in (
                    'i18n_reaction_count', 'reaction_count', 'total_comment_count',
                    'i18n_share_count', 'share_count',
                    'comment_rendering_instance', 'comments_count_summary_renderer',
                )
            ):
                return extract_counts(direct_feedback)
            if contextual_feedbacks:
                def reaction_score(feedback: dict) -> float:
                    rc = feedback.get('reaction_count')
                    if isinstance(rc, dict):
                        rc = rc.get('count')
                    if rc is None:
                        rc = feedback.get('i18n_reaction_count')
                    if rc is None:
                        return -1
                    text = str(rc).strip().upper().replace(',', '')
                    multiplier = 1
                    if text.endswith(('K', 'M', 'B')):
                        multiplier = {'K': 1_000, 'M': 1_000_000, 'B': 1_000_000_000}[text[-1]]
                        text = text[:-1]
                    try:
                        return float(text) * multiplier
                    except (TypeError, ValueError):
                        return 0

                best = max(
                    contextual_feedbacks,
                    key=reaction_score,
                )
                return extract_counts(best)
            return '0', '0', '0'

        post_feedback = Jq.first(post_json, 'comet_ufi_summary_and_actions_renderer')
        if post_feedback and isinstance(post_feedback, dict) and 'feedback' in post_feedback:
            return extract_counts(post_feedback['feedback'])

        fb = Jq.first(post_json, 'feedback')
        if fb and isinstance(fb, dict):
            rc = fb.get('i18n_reaction_count')
            if rc:
                return extract_counts(fb)
            best = best_feedback()
            if best:
                return extract_counts(best)

        best = best_feedback()
        if best:
            return extract_counts(best)

        reactions = Jq.first(post_json, 'i18n_reaction_count') or '0'
        shares = Jq.first(post_json, 'i18n_share_count') or Jq.first(post_json, 'share_count') or '0'
        comments = Jq.first(post_json, 'total_comment_count')
        if not comments:
            cri = Jq.first(post_json, 'comment_rendering_instance')
            if isinstance(cri, dict):
                cnode = cri.get('comments')
                if isinstance(cnode, dict):
                    comments = cnode.get('total_count')
            if not comments:
                ccsr = Jq.first(post_json, 'comments_count_summary_renderer')
                if isinstance(ccsr, dict):
                    fb_inner = ccsr.get('feedback')
                    if isinstance(fb_inner, dict):
                        cri2 = fb_inner.get('comment_rendering_instance')
                        if isinstance(cri2, dict):
                            cnode2 = cri2.get('comments')
                            if isinstance(cnode2, dict):
                                comments = cnode2.get('total_count')
        if not comments:
            comments = '0'

        return str(reactions), str(comments), str(shares)

    @staticmethod
    def get_root_node(post_json: dict, requested_ids: list[str] | None = None) -> dict:
        requested_ids = requested_ids or []

        def selected(key: str, source: dict = post_json):
            return JsonParser.select_requested_candidate(Jq.all(source, key), requested_ids)

        def work_normal_post() -> dict:
            data_blob = selected('data')
            if not isinstance(data_blob, dict):
                short_form = selected('short_form_video_context')
                if short_form:
                    return {'creation_story': short_form}
                return {}
            if 'comet_ufi_summary_and_actions_renderer' in data_blob:
                return data_blob
            elif 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
                node_v2 = data_blob['node_v2']
                if 'comet_sections' in node_v2 or 'creation_story' in node_v2:
                    return node_v2
            elif 'node' in data_blob and isinstance(data_blob['node'], dict):
                node = data_blob['node']
                if 'comet_sections' in node or 'creation_story' in node:
                    return node
            short_form = selected('short_form_video_context', data_blob)
            if short_form:
                return {'creation_story': short_form}
            return {}

        def work_group_post() -> dict:
            hoisted_feed = selected('group_hoisted_feed')
            if isinstance(hoisted_feed, dict):
                if 'comet_sections' in hoisted_feed or 'creation_story' in hoisted_feed:
                    return hoisted_feed
                node_v2 = selected('node_v2', hoisted_feed)
                if isinstance(node_v2, dict):
                    return node_v2

            data_blob = selected('data')
            if isinstance(data_blob, dict):
                group = data_blob.get('group')
                if isinstance(group, dict):
                    if 'comet_sections' in group or 'creation_story' in group:
                        return group
                    node_v2 = selected('node_v2', group)
                    if isinstance(node_v2, dict):
                        return node_v2
            return {}

        methods: list[Callable[[], dict]] = [work_normal_post, work_group_post]

        for method in methods:
            try:
                ret = method()
                if ret:
                    return ret
            except (StopIteration, KeyError):
                continue

        data_blob = selected('data')
        if isinstance(data_blob, dict):
            if 'creation_story' in data_blob and 'feedback' in data_blob:
                return data_blob
            if 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict):
                return data_blob['node_v2']

        raise ParseException('Cannot process post')

    @staticmethod
    def ensure_full_url(u: str) -> str:
        value = str(u)
        parsed = urlparse(value)
        if not parsed.netloc and re.match(r'^(?:www\.|web\.|m\.|mbasic\.)?facebook\.com/', value, re.IGNORECASE):
            parsed = urlparse(f'https://{value}')
        if parsed.netloc:
            hostname = (parsed.hostname or '').lower()
            if hostname == 'facebook.com' or hostname.endswith('.facebook.com'):
                suffix = parsed.path.lstrip('/')
                if parsed.query:
                    suffix += f'?{parsed.query}'
                return f'{WWWFB}/{suffix}'
            return value
        return f'{WWWFB}/{value.removeprefix("/")}'

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            raw_ids = JsonParser.get_requested_ids(post_path)
            requested_ids, route_proven_ids = JsonParser.get_target_ids(html_parser, post_path)
            effective_post_path = post_path
            if (
                raw_ids
                and route_proven_ids
                and not set(raw_ids).intersection(route_proven_ids)
                and cffi.selected_get_response is not None
            ):
                selected_url = str(
                    getattr(cffi.selected_get_response, 'url', '') or ''
                )
                if Utils.facebook_path_segments(selected_url):
                    effective_post_path = selected_url
            if Utils.is_share_path(post_path) and not raw_ids and not route_proven_ids:
                raise NoDataException(
                    'Facebook response did not identify the requested share post'
                )
            required_ids = route_proven_ids or raw_ids
            post_json = JsonParser.get_root_node(
                JsonParser.get_post_json(
                    html_parser,
                    post_path,
                    requested_ids,
                    required_ids,
                ),
                requested_ids,
            )
            if required_ids and not JsonParser.contains_target_id(post_json, required_ids):
                raise NoDataException('Facebook response selected a different post')
            top_reaction_ids: list[str] = []
            likes, cmts, shares = JsonParser.get_interaction_counts(
                post_json, requested_ids, top_reaction_ids
            )

            post_date = -1
            t = Jq.first(post_json, 'creation_time') or Jq.first(post_json, 'created_time')
            if t:
                try:
                    post_date = int(t)
                except (ValueError, TypeError):
                    pass
            if post_date == -1:
                blocks = JsonParser.get_json_blocks(html_parser)
                t = (
                    JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
                    or JsonParser.select_requested_field(blocks, 'created_time', requested_ids)
                )
                if t:
                    try:
                        post_date = int(t)
                    except (ValueError, TypeError):
                        pass

            story_dict = post_json
            if 'content' in post_json and isinstance(post_json['content'], dict) and 'story' in post_json['content']:
                story_dict = post_json['content']['story']
            elif 'creation_story' in post_json:
                story_dict = post_json['creation_story']
                if 'owner' in post_json and ('actors' not in story_dict or not story_dict['actors']):
                    story_dict['actors'] = [post_json['owner']]
            elif 'comet_sections' in post_json:
                sections = post_json['comet_sections']
                if isinstance(sections, dict) and 'content' in sections and isinstance(sections['content'], dict) and 'story' in sections['content']:
                    story_dict = sections['content']['story']
                elif 'feedback' in post_json:
                    pass

            story = Story(story_dict)
            parsed_post_path = urlparse(
                JsonParser.ensure_full_url(effective_post_path)
            ).path
            if not story.video_links and re.search(
                r'/(?:reel|watch|videos|v)(?:/|$)', parsed_post_path, re.IGNORECASE
            ):
                try:
                    story.video_links.append(
                        ReelsParser.get_video_link(
                            html_parser, requested_ids=requested_ids
                        )
                    )
                except FacebedException:
                    pass
            canonical = html_parser.find('link', attrs={'rel': 'canonical'})
            canonical_url = str(canonical.get('href', '')) if canonical else ''
            parsed_canonical = urlparse(canonical_url)
            canonical_host = (parsed_canonical.hostname or '').lower()
            validated_identity_ids = set(route_proven_ids or raw_ids)
            identity_fields = {
                'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
                'storyfbid', 'fbid', 'legacy_fbid', 'story_id', 'storyid',
                'top_level_post_id', 'mf_story_key', 'feedback_id',
            }
            def collect_identity_ids(value) -> None:
                if not isinstance(value, dict):
                    return
                for key, item in value.items():
                    key_name = str(key).lower()
                    if key_name in identity_fields and isinstance(item, (str, int)):
                        validated_identity_ids.add(str(item))
                    elif key_name in identity_fields and isinstance(item, list):
                        validated_identity_ids.update(
                            str(part) for part in item
                            if isinstance(part, (str, int))
                        )

            collect_identity_ids(post_json)
            collect_identity_ids(story_dict)
            story_url_ids = set(JsonParser.get_requested_ids(story.url))
            canonical_ids = set(JsonParser.get_requested_ids(canonical_url))
            if (
                canonical_host == 'facebook.com'
                or canonical_host.endswith('.facebook.com')
            ) and canonical_ids.intersection(validated_identity_ids):
                parts = [part for part in parsed_canonical.path.split('/') if part]
                if 'posts' in parts:
                    post_index = parts.index('posts')
                    numeric_ids = [part for part in parts[post_index + 1:] if part.isdigit()]
                    if numeric_ids:
                        parts = [*parts[:post_index + 1], numeric_ids[-1]]
                        parsed_canonical = parsed_canonical._replace(
                            path='/' + '/'.join(parts),
                            query='',
                            fragment='',
                        )
                post_url = parsed_canonical.geturl()
            elif story.url and story_url_ids.intersection(validated_identity_ids):
                post_url = story.url or JsonParser.ensure_full_url(post_path)
            else:
                post_url = JsonParser.ensure_full_url(effective_post_path)
            post_content = story.get_text()
            post_group_name = JsonParser.get_group_name(html_parser, requested_ids)
            post_author_name = story.author_name
            link_header = f'{post_author_name}' + (f' • {post_group_name}' if post_group_name else '')

            if story.author_id in config['banned_users']:
                return banned(post_url)

            # TODO: support normal /watch here
            return ParsedPost(link_header, post_content.strip(), story.image_links, post_url, post_date,
                              likes, cmts, shares, story.video_links,
                              top_reaction_ids=tuple(top_reaction_ids))


class SinglePhotoParser:
    @staticmethod
    def _select_node(
        candidates: list[dict],
        requested_ids: list[str],
        error_message: str,
    ) -> dict:
        ordered = (
            sorted(candidates, key=lambda item: len(str(item)))
            if requested_ids else candidates
        )
        selected = JsonParser.select_requested_candidate(ordered, requested_ids)
        if selected is not None:
            return selected
        if candidates and requested_ids:
            raise NoDataException('Facebook response did not contain the requested photo')
        raise ParseException(error_message)

    @staticmethod
    def get_content_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'message_preferred_body', 'container_story'):
                candidates.append(bloc)
        selected_block = SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'Cannot process post (cn)',
        )
        data_nodes = [
            node for node in Jq.all(selected_block, 'data') if isinstance(node, dict)
        ]
        selected_data = JsonParser.select_requested_candidate(
            data_nodes, requested_ids or []
        )
        if selected_data is not None:
            return selected_data
        if len(data_nodes) == 1 and not requested_ids:
            return data_nodes[0]
        if len(data_nodes) == 1 and JsonParser.contains_exact_id(
            selected_block, requested_ids or []
        ):
            return data_nodes[0]
        raise NoDataException('Facebook response did not contain the requested photo')

    @staticmethod
    def get_interactions_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict | None:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'comet_ufi_summary_and_actions_renderer'):
                candidates.append(bloc)
        try:
            return SinglePhotoParser._select_node(
                candidates,
                requested_ids or [],
                'Cannot process post (in)',
            )
        except NoDataException:
            if len(candidates) == 1:
                return None
            raise

    @staticmethod
    def get_single_image(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> str:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'prefetch_uris_v2'):
                candidates.append(bloc)
        selected_block = SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'cannot find single image',
        )
        prefetch = Jq.first(selected_block, 'prefetch_uris_v2')
        if not isinstance(prefetch, list) or not prefetch:
            raise ParseException('cannot find single image')
        return str(prefetch[0]['uri'])

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _route_proven_ids = JsonParser.get_target_ids(
                html_parser, post_path
            )
            content_node = SinglePhotoParser.get_content_node(html_parser, requested_ids)
            interaction_ids = list(requested_ids)
            identity_keys = {
                'id', 'video_id', 'post_id', 'story_fbid', 'fbid',
                'legacy_fbid', 'top_level_post_id', 'mf_story_key', 'feedback_id',
            }
            identity_sources = [content_node]
            for key in ('container_story', 'creation_story'):
                source = Jq.first(content_node, key)
                if isinstance(source, dict):
                    identity_sources.append(source)
            for source in identity_sources:
                for key, value in source.items():
                    if str(key).lower() in identity_keys and isinstance(value, (str, int)):
                        text = str(value)
                        interaction_ids.append(text)
                        try:
                            decoded = base64.b64decode(
                                text + ('=' * (-len(text) % 4)), validate=True
                            ).decode('utf-8')
                        except (ValueError, UnicodeDecodeError):
                            continue
                        numeric_parts = re.findall(r'\d+', decoded)
                        if numeric_parts:
                            interaction_ids.append(numeric_parts[-1])
            interaction_ids = list(dict.fromkeys(interaction_ids))
            interaction_node = SinglePhotoParser.get_interactions_node(
                html_parser, interaction_ids
            )

            post_text = content_node['message']['text'] if content_node['message'] and 'text' in content_node['message'] else ''
            post_author = content_node['owner']['name']
            post_date = content_node['created_time']
            if interaction_node is None:
                likes, cmts, shares = '0', '0', '0'
                top_reaction_ids: list[str] = []
            else:
                top_reaction_ids = []
                likes, cmts, shares = JsonParser.get_interaction_counts(
                    interaction_node, interaction_ids, top_reaction_ids
                )
            image_url = SinglePhotoParser.get_single_image(html_parser, requested_ids)

            return ParsedPost(post_author, post_text.strip(), [image_url], JsonParser.ensure_full_url(post_path),
                              post_date, likes, cmts, shares, [],
                              top_reaction_ids=tuple(top_reaction_ids))


class PhotocomParser:
    @staticmethod
    def get_content_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'attached_comment') and not Jq.has(bloc, 'unified_reactors'):
                candidates.extend(
                    node for node in Jq.all(bloc, 'result') if isinstance(node, dict)
                )
        return SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'Cannot process photocom (cn)',
        )

    @staticmethod
    def get_media_node(
        html_parser: BeautifulSoup,
        requested_ids: list[str] | None = None,
    ) -> dict:
        candidates = [
            bloc for bloc in JsonParser.get_json_blocks(html_parser)
            if Jq.has(bloc, 'attached_comment', 'unified_reactors')
        ]
        return SinglePhotoParser._select_node(
            candidates,
            requested_ids or [],
            'Cannot process photocom (media)',
        )

    @staticmethod
    def get_reaction_count(media_node: dict) -> int:
        feedback = PhotocomParser.get_reaction_feedback(media_node)
        reactors = feedback.get('unified_reactors')
        if not isinstance(reactors, dict) or 'count' not in reactors:
            reactors = Jq.first(media_node, 'unified_reactors')
        if isinstance(reactors, dict) and 'count' in reactors:
            return reactors['count']
        raise ParseException('Cannot process photocom (rc)')

    @staticmethod
    def get_reaction_feedback(media_node: dict) -> dict:
        cur = Jq.first(media_node, 'currMedia')
        if isinstance(cur, dict):
            attached_comment = cur.get('attached_comment')
            if isinstance(attached_comment, dict):
                feedback = attached_comment.get('feedback')
                if isinstance(feedback, dict):
                    return feedback
        raise ParseException('Cannot process photocom (rf)')

    @staticmethod
    def get_attached_image_and_url(media_node: dict) -> tuple[str, str]:
        cur = Jq.first(media_node, 'currMedia')
        if isinstance(cur, dict):
            return str(cur['image']['uri']), str(cur['attached_comment']['feedback']['url'])
        raise ParseException('Cannot process photocom (iau)')

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _route_proven_ids = JsonParser.get_target_ids(
                html_parser, post_path
            )
            content_node = PhotocomParser.get_content_node(html_parser, requested_ids)
            media_node = PhotocomParser.get_media_node(html_parser, requested_ids)
            body = content_node['data']['attached_comment']['preferred_body']

            op_name = content_node['data']['owner']['name'] + ' (💬)'
            post_text = '' if body is None else body['text']
            post_time = content_node['data']['created_time']
            post_image, post_url = PhotocomParser.get_attached_image_and_url(media_node)
            reaction_count = PhotocomParser.get_reaction_count(media_node)
            top_reaction_ids = Utils.get_top_reaction_ids(
                PhotocomParser.get_reaction_feedback(media_node)
            )

            return ParsedPost(op_name, post_text, [post_image], post_url, post_time,
                              Utils.human_format(reaction_count), 'null', 'null', [],
                              top_reaction_ids=top_reaction_ids)


class ReelsParser:
    @staticmethod
    def get_video_link(
        html_parser: BeautifulSoup | None,
        user_node: dict = None,
        requested_ids: list[str] | None = None,
    ) -> str:
        def work_node(node: dict) -> str:
            video_node = node
            if not (Jq.first(video_node, 'browser_native_hd_url') or Jq.first(video_node, 'browser_native_sd_url')):
                video_node = Jq.first(node, 'videoDeliveryLegacyFields')
            for key in ['browser_native_hd_url', 'browser_native_sd_url']:
                try:
                    video_link = Jq.first(video_node, key)
                    if not video_link or not Utils.is_safe_media_url(str(video_link)):
                        continue
                    return str(video_link)
                except StopIteration:
                    pass
            raise ParseException('Invalid reels link (vn)')

        if user_node:
            return work_node(user_node)

        candidates = JsonParser.get_video_delivery_nodes(
            JsonParser.get_json_blocks(html_parser)
        )

        requested_ids = requested_ids or []
        requested_id_set = set(map(str, requested_ids))
        matching = [
            node for node in candidates
            if requested_id_set.intersection(
                JsonParser.direct_video_identity_ids(node)
            )
        ]
        for node in matching:
            try:
                return work_node(node)
            except (ParseException, KeyError, TypeError, IndexError):
                continue
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        for node in candidates:
            try:
                return work_node(node)
            except (ParseException, KeyError, TypeError, IndexError):
                continue

        raise ParseException('Invalid reels link (vn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup, requested_ids: list[str] | None = None) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            candidates.extend(Jq.all(bloc, 'creation_story'))
            candidates.extend(Jq.all(bloc, 'short_form_video_context'))
        candidates = [node for node in candidates if isinstance(node, dict)]

        requested_ids = requested_ids or []
        for node in candidates:
            if JsonParser.contains_exact_id(node, requested_ids):
                return node
        for node in candidates:
            if JsonParser.contains_target_id(node, requested_ids):
                return node
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        if candidates:
            return candidates[0]
        raise ParseException('Invalid reels link (cn)')

    @staticmethod
    def get_reaction_counts(
        html_parser: BeautifulSoup,
        is_ig: bool,
        video_id: str,
        related_ids: list[str] | None = None,
    ) -> tuple[str, str, str]:
        target_ids = list(dict.fromkeys(
            str(value) for value in [video_id, *(related_ids or [])] if value
        ))
        direct_blocks: list[dict] = []
        url_blocks: list[dict] = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if not Jq.has(bloc, 'unified_reactors'):
                continue
            if JsonParser.contains_exact_id(bloc, target_ids):
                direct_blocks.append(bloc)
            elif JsonParser.contains_target_id(bloc, target_ids):
                url_blocks.append(bloc)

        blocks = direct_blocks or url_blocks

        if len(blocks) == 0:
            raise ParseException('Cannot process post (cn)')

        contextual_feedbacks: list[tuple[set[str], dict]] = []
        id_keys = {
            'id', 'video_id', 'videoid', 'post_id', 'postid', 'story_fbid',
            'storyfbid', 'fbid', 'legacy_fbid', 'top_level_post_id',
        }

        def walk(value, inherited_ids: set[str] | None = None):
            inherited_ids = inherited_ids or set()
            if isinstance(value, dict):
                own_ids = set()
                for key, item in value.items():
                    if str(key).lower() not in id_keys:
                        continue
                    if isinstance(item, (str, int)):
                        own_ids.add(str(item))
                    elif isinstance(item, list):
                        own_ids.update(str(part) for part in item if isinstance(part, (str, int)))
                context_ids = own_ids or inherited_ids
                feedback = value.get('feedback')
                if isinstance(feedback, dict):
                    contextual_feedbacks.append((set(context_ids), feedback))
                elif isinstance(feedback, list):
                    contextual_feedbacks.extend(
                        (set(context_ids), item) for item in feedback if isinstance(item, dict)
                    )
                for item in value.values():
                    walk(item, context_ids)
            elif isinstance(value, list):
                for item in value:
                    walk(item, inherited_ids)

        for block in blocks:
            walk(block)

        target_id_set = set(target_ids)
        feedbacks: list[tuple[set[str], dict]] = []
        seen = set()
        for context_ids, feedback in contextual_feedbacks:
            if not context_ids.intersection(target_id_set) and not JsonParser.contains_target_id(feedback, target_ids):
                continue
            marker = id(feedback)
            if marker not in seen:
                seen.add(marker)
                feedbacks.append((context_ids, feedback))

        if feedbacks:
            def unified_count(item: tuple[set[str], dict]) -> int:
                count = item[1].get('unified_reactors', {}).get('count', 0)
                try:
                    return int(count)
                except (TypeError, ValueError):
                    return 0

            first_context, first_fb = max(feedbacks, key=unified_count)
            context_companions = [
                feedback for context_ids, feedback in feedbacks
                if feedback is not first_fb
                and (not first_context or context_ids.intersection(first_context))
            ]
            first_feedback_id = first_fb.get('id')
            companions = [
                feedback for feedback in context_companions
                if first_feedback_id and feedback.get('id') == first_feedback_id
            ] or context_companions
            last_fb = next((
                fb for fb in reversed(companions)
                if 'total_comment_count' in fb
                or 'share_count_reduced' in fb
                or 'share_count' in fb
            ), None)
            if last_fb is None:
                last_fb = next((
                    fb for fb in reversed(companions)
                    if 'cross_universe_feedback_info' in fb
                ), first_fb)
        else:
            raise ParseException('Cannot associate reactions with requested video')

        cross_info = last_fb.get('cross_universe_feedback_info', {})
        ig_cmts = cross_info.get('ig_comment_count') or last_fb.get('total_comment_count', 0)
        likes = first_fb.get('unified_reactors', {}).get('count', 0)
        cmts = ig_cmts if is_ig else last_fb.get('total_comment_count', 0)
        shares = last_fb.get('share_count_reduced', last_fb.get('share_count', 0))

        return Utils.human_format(likes), Utils.human_format(cmts), Utils.human_format(shares)


    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _strong_ids = JsonParser.get_target_ids(html_parser, post_path)
            content_node = ReelsParser.get_content_node(html_parser, requested_ids)

            video_link = ReelsParser.get_video_link(html_parser, requested_ids=requested_ids)
            video_id = next(
                (
                    requested_id for requested_id in requested_ids
                    if JsonParser.contains_target_id(content_node, [requested_id])
                ),
                None,
            )
            video_id = video_id or content_node.get('video', {}).get('id') or content_node.get('id')
            if not video_id:
                video_id = Jq.first(content_node, 'id')

            owner_info = content_node.get('short_form_video_context', {}).get('video_owner') or content_node.get('video_owner')
            if not owner_info:
                owner_info = Jq.first(content_node, 'video_owner')

            is_ig = owner_info['__typename'].startswith('InstagramUser')
            op_name = ('📷 @' if is_ig else '') + owner_info['username' if is_ig else 'name']
            post_url = content_node.get('short_form_video_context', {}).get('shareable_url') or JsonParser.ensure_full_url(post_path)

            post_date = content_node.get('creation_time')
            if not post_date:
                blocks = JsonParser.get_json_blocks(html_parser)
                post_date = (
                    JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
                    or JsonParser.select_requested_field(blocks, 'created_time', requested_ids)
                )
                try:
                    post_date = int(post_date) if post_date else None
                except (ValueError, TypeError):
                    post_date = None
            if not post_date:
                post_date = -1

            post_text = '' if content_node.get('message') is None else content_node['message']['text']

            reaction_ids = [video_id, content_node.get('id'), content_node.get('post_id')]
            content_feedback = content_node.get('feedback')
            if isinstance(content_feedback, dict):
                reaction_ids.append(content_feedback.get('id'))
            likes, cmts, shares = ReelsParser.get_reaction_counts(
                html_parser, is_ig, video_id, reaction_ids
            )

            if owner_info['id'] in config['banned_users']:
                return banned(post_url)

            return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


class VideoWatchParser:
    # excluding group post video since they are handled by jsonparser
    @staticmethod
    def get_op_name(
        html_parser: BeautifulSoup,
        content_node: dict | None = None,
        requested_ids: list[str] | None = None,
    ) -> str:
        if content_node:
            owner = content_node.get('owner') or Jq.first(content_node, 'owner')
            if isinstance(owner, dict) and owner.get('name'):
                return owner['name']
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = requested_ids or []
        owner = JsonParser.select_requested_field(blocks, 'owner', requested_ids)
        if isinstance(owner, dict) and owner.get('name'):
            return owner['name']
        raise ParseException('Invalid watch link (opn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup, requested_ids: list[str] | None = None) -> dict:
        candidates = []
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc,'comment_rendering_instance', 'video_view_count_renderer'):
                for result in Jq.all(bloc, 'result'):
                    if isinstance(result, dict) and isinstance(result.get('data'), dict):
                        candidates.append(result['data'])

        requested_ids = requested_ids or []
        for node in candidates:
            if JsonParser.contains_exact_id(node, requested_ids):
                return node
        for node in candidates:
            if JsonParser.contains_target_id(node, requested_ids):
                return node
        if requested_ids:
            raise NoDataException('Facebook response did not contain the requested video')
        if candidates:
            return candidates[0]
        canonical = html_parser.find('link', attrs={'rel': 'canonical'})
        if canonical:
            canonical_path = urlparse(canonical.get('href', '')).path.rstrip('/')
            if canonical_path == '/watch':
                raise NoDataException('Facebook served generic watch feed instead of specific video')
        raise ParseException('Invalid watch link (cn)')

    @staticmethod
    def get_date(
        html_parser: BeautifulSoup,
        content_node: dict | None = None,
        requested_ids: list[str] | None = None,
    ) -> int:
        if content_node:
            creation_time = content_node.get('creation_time') or Jq.first(content_node, 'creation_time')
            if creation_time:
                return int(creation_time)
        blocks = JsonParser.get_json_blocks(html_parser)
        requested_ids = requested_ids or []
        creation_time = JsonParser.select_requested_field(blocks, 'creation_time', requested_ids)
        if creation_time:
            return int(creation_time)
        raise ParseException('cannot find date')

    @staticmethod
    def process_post(
        post_path: str,
        http_response: CffiResponse | None = None,
    ) -> ParsedPost:
        if http_response is None:
            page = JsonParser.fetch_page(post_path)
        else:
            page = JsonParser.fetch_page(post_path, http_response=http_response)
        with page as html_parser:
            requested_ids, _strong_ids = JsonParser.get_target_ids(html_parser, post_path)
            content_node = VideoWatchParser.get_content_node(html_parser, requested_ids)

            video_link = ReelsParser.get_video_link(html_parser, requested_ids=requested_ids)

            post_url = JsonParser.ensure_full_url(post_path)
            op_name = VideoWatchParser.get_op_name(html_parser, content_node, requested_ids)
            post_text = content_node['title']['text'] if (content_node.get('title') and isinstance(content_node['title'], dict) and content_node['title'].get('text')) else ''
            if not post_text:
                msg = Jq.first(content_node, 'message')
                if isinstance(msg, dict):
                    post_text = msg.get('text', '')

            post_feedback = content_node['feedback']
            likes = Utils.human_format(post_feedback['reaction_count']['count'])
            shares = 'null'
            cmts = Utils.human_format(post_feedback['total_comment_count'])
            post_date = VideoWatchParser.get_date(html_parser, content_node, requested_ids)
            top_reaction_ids = Utils.get_top_reaction_ids(post_feedback)

            return ParsedPost(op_name, post_text, [], post_url, post_date,
                              likes, cmts, shares, [video_link],
                              top_reaction_ids=top_reaction_ids)


def format_error_message_embed(original_url: str) -> str:
    return Utils.prettify(f'''<!DOCTYPE html>
<html lang="">
<head>
<meta charset="UTF-8" />
    <meta name="theme-color" content="#2c3048f" />
    <meta property="og:title" content="Log in or sign up to view"/>
    <meta property="og:description" content="See posts, photos and more on Facebook."/>
    <meta http-equiv="refresh" content="0;url={escape(quote(original_url), quote=True)}"/>
</head>
</html>''')


def failed_post_type(path: str, upstream_response: CffiResponse | None, reason: str) -> str:
    if reason.startswith('unsupported_') and reason.endswith('_target'):
        return reason.removeprefix('unsupported_').removesuffix('_target').replace('media_set', 'album')
    if reason == 'private_group_post_unavailable':
        return 'group_post'
    if reason == 'target_video_payload_missing':
        return 'reel'
    paths = [path]
    if upstream_response is not None:
        paths.insert(0, str(upstream_response.url))
    for candidate in paths:
        parts = Utils.facebook_path_segments(candidate)
        if not parts or Utils.is_control_path(candidate, 'login', 'checkpoint', 'recover'):
            continue
        if parts[0] == 'groups':
            return 'group_post'
        if parts[0] == 'share':
            return 'share'
        kinds = {
            'reel': 'reel', 'videos': 'video', 'watch': 'video', 'v': 'video',
            'posts': 'post', 'permalink': 'post', 'story.php': 'post',
            'permalink.php': 'post', 'photo.php': 'photo', 'photo': 'photo',
            'photos': 'photo', 'marketplace': 'marketplace', 'media': 'album',
        }
        if parts[0] in kinds:
            return kinds[parts[0]]
        if len(parts) > 1 and parts[1] in {'posts', 'videos', 'photos', 'v'}:
            return kinds[parts[1]]
        if len(parts) == 1 or parts[0] in {'people', 'p'}:
            return 'profile'
    return 'unknown'


def is_facebook_url(url: str) -> bool:
    if urlparse(url).netloc:
        url = Utils.normalize_facebook_path(url)
    wwwfb = f'{WWWFB}/'
    username_pattern = '[a-zA-Z0-9-._]*'  # also covers /watch
    full_url = f'{wwwfb}{url}'
    parsed_url = urlparse(full_url)

    is_group_post = re.match(f'^/groups/{username_pattern}', parsed_url.path)
    is_permalink = parsed_url.path.startswith('/permalink.php')
    is_story = parsed_url.path.startswith('/story.php')
    is_post = re.match(f'/{username_pattern}/posts', parsed_url.path)
    is_photo = parsed_url.path.startswith('/photo')
    is_photo_album = re.match(f'/{username_pattern}/photos(?:/|$)', parsed_url.path)

    return is_permalink or is_post or is_story or is_photo or is_photo_album or is_group_post


def format_reel_post_embed(post: ParsedPost) -> str:
    def get_video_meta_tag(link: str) -> str:
        escaped_link = escape(link, quote=True)
        return '\n'.join([
            f'<meta property="twitter:player:stream" content="{escaped_link}"/>',
            f'<meta property="og:video" content="{escaped_link}"/>',
            f'<meta property="og:video:secure_url" content="{escaped_link}"/>',
        ])

    video_meta_tags = '\n'.join([get_video_meta_tag(vu) for vu in post.video_links])
    reaction_str = Utils.format_reactions_str(
        post.likes, post.comments, post.shares, post.top_reaction_ids
    )
    post_date = Utils.timestamp_to_str(post.date)
    site_name = escape(f'{get_credit()}\n{post_date}\n{reaction_str}', quote=True)
    color = '#0866ff'

    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{site_name}"/>
            <meta property="og:url" content="{escape(quote(post.url), quote=True)}"/>
            <meta property="og:video:type" content="video/mp4"/>
            <meta property="twitter:player:stream:content_type" content="video/mp4"/>

            {video_meta_tags}

            <link rel="canonical" href="{escape(quote(post.url), quote=True)}"/>
            <meta http-equiv="refresh" content="0;url={escape(quote(post.url), quote=True)}"/>
            <meta name="twitter:card" content="player"/>
            <meta name="theme-color" content="{color}"/>
        </head>
        </html>''')


def format_full_post_embed(post: ParsedPost) -> str:
    if post.video_links:
        return format_reel_post_embed(post)
    image_links = post.image_links
    image_counter = f'\ncontains 4+ images' if len(image_links) > 4 else ''
    image_links = image_links[:4]
    image_meta_tags = '\n'.join([
        f'<meta property="og:image" content="{escape(iu, quote=True)}"/>'
        for iu in image_links
    ])
    post_date = Utils.timestamp_to_str(post.date)
    reaction_str = Utils.format_reactions_str(
        post.likes, post.comments, post.shares, post.top_reaction_ids
    )
    site_name = escape(
        f'{get_credit()}\n{post_date}\n{reaction_str}{image_counter}', quote=True
    )

    # TODO: organize and duplicate the neccessary tags
    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{site_name}"/>
            <meta property="og:url" content="{escape(quote(post.url), quote=True)}"/>
            {image_meta_tags}
            <link rel="canonical" href="{escape(quote(post.url), quote=True)}"/>
            <meta http-equiv="refresh" content="0;url={escape(quote(post.url), quote=True)}"/>
            <meta name="twitter:card" content="summary_large_image"/>
            <meta name="theme-color" content="#0866ff"/>
        </head>
        </html>''')


def format_redirect_page(url: str) -> str:
    script_url = json.dumps(url).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return Utils.prettify(f'''<!DOCTYPE HTML>
<html lang="en-US">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="refresh" content="0; url={escape(quote(url), quote=True)}">
        <script type="text/javascript">
            window.location.href = {script_url}
        </script>
        <title>redirecting...</title>
    </head>
    <body>
    </body>
</html>''')


def _invoke_parser(parser, post_path: str, http_response: CffiResponse | None = None):
    if http_response is None:
        return parser(post_path)
    return parser(post_path, http_response=http_response)


PARSER_FALLBACK_EXCEPTIONS = (
    NoDataException,
    ParseException,
    KeyError,
    TypeError,
    IndexError,
    ValueError,
    AttributeError,
)
ROUTE_FALLBACK_EXCEPTIONS = PARSER_FALLBACK_EXCEPTIONS + (UpstreamException,)


def _allow_route_upstream_fallback(error: Exception) -> bool:
    if isinstance(error, UnsupportedRouteException):
        return False
    if isinstance(error, NoDataException) and getattr(error, 'terminal', False):
        return False
    return not isinstance(error, UpstreamException) or error.status_code in {
        403, 404, 500, 502, 503, 504,
    }


def _as_parser_error(error: Exception) -> FacebedException:
    if isinstance(error, FacebedException):
        return error
    return ParseException(f'{type(error).__name__}: {error}')


def _prefer_parser_error(first: Exception, second: Exception) -> FacebedException:
    first = _as_parser_error(first)
    second = _as_parser_error(second)
    for error in (first, second):
        if isinstance(error, UnsupportedRouteException):
            return error
    for error in (first, second):
        if (
            isinstance(error, NoDataException)
            and getattr(error, 'reason_code', 'no_data') != 'no_data'
        ):
            return error
    if isinstance(second, ParseException) and second.html:
        return second
    if isinstance(first, ParseException) and first.html:
        return first
    return second


def _parse_with_generic(parser, post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    try:
        return _invoke_parser(parser, post_path, http_response)
    except PARSER_FALLBACK_EXCEPTIONS as first_error:
        if not _allow_route_upstream_fallback(first_error):
            raise
        try:
            return _invoke_parser(JsonParser.process_post, post_path, http_response)
        except PARSER_FALLBACK_EXCEPTIONS as second_error:
            raise _prefer_parser_error(first_error, second_error)


def _parse_video_path(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    parsed = urlparse(post_path)
    video_id = JsonParser.get_terminal_video_id(post_path)
    if not video_id:
        search = re.search(
            r'(?:^|/)(?:[^/]+/)?v/(\d+)(?:/|$)',
            parsed.path,
            re.IGNORECASE,
        )
        video_id = search.group(1) if search else None
    if not video_id:
        return _invoke_parser(JsonParser.process_post, post_path, http_response)

    path_parts = [part for part in parsed.path.split('/') if part]
    videos_index = next(
        (
            index for index, part in enumerate(path_parts)
            if part.casefold() == 'videos'
        ),
        None,
    )
    video_parts = (
        path_parts[videos_index + 1:]
        if videos_index is not None else []
    )
    slugged_video_path = bool(
        len(video_parts) >= 2
        and not video_parts[0].casefold().startswith('pcb.')
    )

    original_response = http_response
    generic_post = None
    generic_error = None
    if slugged_video_path:
        try:
            generic_post = _invoke_parser(
                JsonParser.process_post, post_path, original_response
            )
            if generic_post.video_links:
                return generic_post
        except ROUTE_FALLBACK_EXCEPTIONS as error:
            generic_error = error
            if not _allow_route_upstream_fallback(error):
                raise

    reel_path = f'reel/{video_id}'
    if parsed.query:
        reel_path += f'?{parsed.query}'
    reel_response = http_response
    if http_response is not None:
        final_path = Utils.normalize_facebook_path(str(getattr(http_response, 'url', '')))
        if urlparse(final_path).path.rstrip('/') != urlparse(reel_path).path.rstrip('/'):
            reel_response = None
    try:
        return _invoke_parser(ReelsParser.process_post, reel_path, reel_response)
    except ROUTE_FALLBACK_EXCEPTIONS as reel_error:
        if not _allow_route_upstream_fallback(reel_error):
            raise
        if not slugged_video_path:
            try:
                generic_post = _invoke_parser(
                    JsonParser.process_post, post_path, original_response
                )
                if generic_post.video_links:
                    return generic_post
            except ROUTE_FALLBACK_EXCEPTIONS as error:
                generic_error = error
                if not _allow_route_upstream_fallback(error):
                    raise
        try:
            return _invoke_parser(
                VideoWatchParser.process_post, post_path, original_response
            )
        except ROUTE_FALLBACK_EXCEPTIONS as watch_error:
            if generic_post is not None:
                return generic_post
            raise _prefer_parser_error(
                _prefer_parser_error(reel_error, generic_error), watch_error
            )


def _ensure_facebook_page_target(post_path: str) -> None:
    parsed_url = urlparse(post_path)
    if parsed_url.netloc:
        hostname = (parsed_url.hostname or '').lower()
        if hostname != 'facebook.com' and not hostname.endswith('.facebook.com'):
            raise UnsupportedRouteException('refusing to fetch non-Facebook host')


def _dispatch_post(post_path: str, http_response: CffiResponse | None = None) -> ParsedPost:
    _ensure_facebook_page_target(post_path)
    parsed_path = urlparse(post_path).path
    if Utils.is_share_path(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    if (
        re.search(r'(?:^|/)videos/', parsed_path, re.IGNORECASE)
        or re.search(r'(?:^|/)(?:[^/]+/)?v/\d+(?:/|$)', parsed_path, re.IGNORECASE)
    ):
        _mark_parser_pipeline_entry()
        return _parse_video_path(post_path, http_response)
    if re.match(r'^/?reel/[^/?]+', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(ReelsParser.process_post, post_path, http_response)
    if re.match(r'^/?photo(?:\.php)?/?$', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(SinglePhotoParser.process_post, post_path, http_response)
    if re.match(r'^/?watch(?:/|$)', parsed_path, re.IGNORECASE):
        _mark_parser_pipeline_entry()
        return _parse_with_generic(VideoWatchParser.process_post, post_path, http_response)
    if is_facebook_url(post_path):
        _mark_parser_pipeline_entry()
        return _invoke_parser(JsonParser.process_post, post_path, http_response)
    raise UnsupportedRouteException('unsupported Facebook route')


def _successful_embed(parsed_post: ParsedPost) -> str:
    response.status = 200
    response.headers['Cache-Control'] = 'public, max-age=900'
    return format_full_post_embed(parsed_post)


def _send_dump_report(
    path: str,
    http_response: CffiResponse | None = None,
) -> None:
    http_response = http_response or cffi.selected_get_response or cffi.last_get_response
    if http_response is None:
        logging.info('no body-bearing GET captured for dump /%s', path)
        return
    try:
        raw_bytes = bytes(http_response.content or b'')
        filename = re.sub(r'[^a-zA-Z0-9]', '_', path)[:80] + '_dump.html'
        display_path = '/' + path.lstrip('/')
        url = JsonParser.ensure_full_url(path)
        embed = DiscordEmbed(
            title='manual dump report',
            description=f'🔗 [`{display_path}`]({url})\n📋 Manual dump requested by user',
            color='3498DB',
        )
        embed.add_embed_field(name='Attached Payload', value=f'`{filename}`', inline=True)
        embed.add_embed_field(name='Response Size', value=f'{len(raw_bytes)} bytes', inline=True)
        embed.add_embed_field(name='Final URL', value=str(getattr(http_response, 'url', url)), inline=True)
        embed.add_embed_field(name='Status', value=str(getattr(http_response, 'status_code', 'unknown')), inline=True)
        Utils.warn(file_content=raw_bytes, filename=filename, embed=embed)
        logging.info('dump report sent for /%s', path)
    except Exception:
        logging.error("couldn't dump /%s\n%s", path, traceback.format_exc())


def _discord_text(value: object, limit: int = 1024) -> str:
    text = str(value).replace('\x00', '')
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + '…'


def _human_bytes(size: int) -> str:
    amount = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if amount < 1024 or unit == 'GiB':
            precision = 0 if unit == 'B' else 1
            return f'{amount:.{precision}f} {unit}'
        amount /= 1024
    return f'{size} B'


def _restricted_page_attachment(
    origin_url: str,
    reason: str,
    raw_bytes: bytes,
) -> tuple[bytes, str]:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    origin_name = urlparse(origin_url).path.strip('/') or 'facebook'
    stem = re.sub(r'[^a-zA-Z0-9._-]', '_', origin_name)[:52].strip('._-')
    reason_name = re.sub(r'[^a-zA-Z0-9._-]', '_', reason)[:24].strip('._-')
    filename = '_'.join(filter(None, (stem, reason_name, digest[:12])))
    file_content = raw_bytes
    suffix = '.html'
    if len(raw_bytes) > 7 * 1024 * 1024:
        file_content = gzip.compress(raw_bytes, compresslevel=6, mtime=0)
        suffix += '.gz'
    return file_content, (filename or f'facebook_{digest[:12]}') + suffix


def _upstream_redirect_chain(http_response: CffiResponse) -> str:
    chain = [*(getattr(http_response, 'history', None) or []), http_response]
    return '\n'.join(
        f'`{getattr(item, "status_code", "?")}` {_discord_text(getattr(item, "url", "?"), 850)}'
        for item in chain
    )


def _send_restricted_page_report(
    origin_path: str,
    error: NoDataException,
    http_response: CffiResponse | None = None,
) -> None:
    wh = config.get('notifier_webhook', '')
    if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
        return

    try:
        origin_url = JsonParser.ensure_full_url(origin_path)
        reason = str(getattr(error, 'reason_code', 'no_data'))
        title = {
            'login_wall': 'Facebook login wall detected',
            'private_group_post_unavailable': 'Restricted Facebook page detected',
        }.get(reason, 'Facebook content unavailable or restricted')
        embed = DiscordEmbed(
            title=title,
            description=_discord_text(' '.join(str(error).split()), 1500),
            color='ED4245',
        )
        if len(origin_url) <= 2048:
            embed.set_url(origin_url)
        embed.set_timestamp()
        embed.set_footer(text='facebed notifier • exact upstream response attached')
        embed.add_embed_field(
            name='Detection', value=f'`{_discord_text(reason, 120)}`', inline=True
        )
        embed.add_embed_field(
            name='Page type',
            value=f'`{failed_post_type(origin_path, http_response, reason)}`',
            inline=True,
        )
        embed.add_embed_field(
            name='Origin URL', value=_discord_text(origin_url, 900), inline=False
        )

        file_content = None
        filename = None
        if http_response is None:
            embed.add_embed_field(
                name='Upstream response',
                value='No body-bearing upstream response was captured.',
                inline=False,
            )
        else:
            final_url = str(getattr(http_response, 'url', '') or origin_url)
            status = getattr(http_response, 'status_code', 'unknown')
            headers = getattr(http_response, 'headers', {}) or {}
            content_type = (
                headers.get('Content-Type')
                or headers.get('content-type')
                or 'unknown'
            )
            raw_bytes = bytes(getattr(http_response, 'content', b'') or b'')
            file_content, filename = _restricted_page_attachment(
                origin_url, reason, raw_bytes
            )
            embed.add_embed_field(
                name='Upstream response',
                value=(
                    f'HTTP `{status}` • `{_discord_text(content_type, 250)}` • '
                    f'`{_human_bytes(len(raw_bytes))}`'
                ),
                inline=False,
            )
            embed.add_embed_field(
                name='Final upstream URL',
                value=_discord_text(final_url, 900),
                inline=False,
            )
            history = getattr(http_response, 'history', None) or []
            if history:
                embed.add_embed_field(
                    name=f'Redirect chain ({len(history)} hop(s))',
                    value=_discord_text(_upstream_redirect_chain(http_response), 900),
                    inline=False,
                )

        Utils.warn(
            file_content=file_content,
            filename=filename,
            embed=embed,
        )
        logging.info(
            'restricted page report queued reason=%s url=%s bytes=%s',
            reason,
            origin_url,
            len(file_content) if file_content is not None else 0,
        )
    except Exception:
        logging.error(
            "couldn't report restricted page /%s\n%s",
            origin_path,
            traceback.format_exc(),
        )


def _authentication_wall_reason(
    final_url: str,
    html_parser: BeautifulSoup,
) -> str | None:
    if Utils.is_control_path(final_url, 'login'):
        return 'login redirect'
    if Utils.is_control_path(final_url, 'checkpoint'):
        return 'checkpoint redirect'
    if Utils.is_control_path(final_url, 'recover'):
        return 'account recovery redirect'
    route_names = {
        str(route.get('canonicalRouteName', ''))
        for route in JsonParser.get_initial_routes(
            JsonParser.get_json_blocks(html_parser)
        )
    }
    if route_names.intersection({
        'comet.fbweb.CometLoginRoute',
        'comet.fbweb.CometLoginPageRoute',
        'comet.fbweb.CometCheckpointRoute',
        'comet.fbweb.CometRecoveryRoute',
    }):
        return 'authentication route'
    for form in html_parser.find_all('form'):
        if not Utils.is_control_path(str(form.get('action', '')), 'login'):
            continue
        inputs = form.find_all('input')
        input_names = {
            str(node.get('name', '')).casefold() for node in inputs
        }
        input_types = {
            str(node.get('type', '')).casefold() for node in inputs
        }
        if 'password' in input_types and input_names.intersection({
            'email', 'pass', 'password'
        }):
            return 'login form'
    return None

_parser_pipeline_local = threading.local()


@contextmanager
def _parser_pipeline_scope():
    previous = getattr(_parser_pipeline_local, 'state', None)
    state = {'entered': False}
    _parser_pipeline_local.state = state
    try:
        yield state
    finally:
        if previous is None:
            delattr(_parser_pipeline_local, 'state')
        else:
            _parser_pipeline_local.state = previous


def _mark_parser_pipeline_entry() -> None:
    state = getattr(_parser_pipeline_local, 'state', None)
    if state is not None and not state['entered']:
        state['entered'] = True
        service_metrics.record_request()


def _run_tracked_request(
    path: str,
) -> tuple[str, CffiResponse | None, bool]:
    with _parser_pipeline_scope() as state:
        try:
            result, selected_response = _run_request(path)
        except Exception as exc:
            setattr(exc, 'parser_pipeline_entered', state['entered'])
            raise
        return result, selected_response, state['entered']


def _record_scrape_error(error: Exception) -> None:
    if isinstance(error, UnsupportedRouteException):
        return
    parser_entered = bool(getattr(error, 'parser_pipeline_entered', False))
    if not parser_entered and isinstance(error, ShareResolutionException):
        if error.count_as_error:
            service_metrics.record_error()
        return
    if not parser_entered:
        service_metrics.record_request()
    service_metrics.record_error()


def _attach_attempt_response(
    error: Exception,
    selected_response: CffiResponse | None,
) -> None:
    if (
        getattr(error, 'upstream_response', None) is None
        and selected_response is not None
    ):
        setattr(error, 'upstream_response', selected_response)
    setattr(error, 'selected_response', selected_response)


def _run_request(path: str) -> tuple[str, CffiResponse | None]:
    _ensure_facebook_page_target(path)
    with cffi.request_scope():
        try:
            result = _handle_facebook_path(path)
        except Exception as exc:
            selected_response = cffi.selected_get_response or cffi.last_get_response
            _attach_attempt_response(exc, selected_response)
            raise
        return result, cffi.selected_get_response or cffi.last_get_response

def _handle_facebook_path(path: str) -> str:
    prefetched_response = None
    if '3' in request.query.getall('type'):
        try:
            _mark_parser_pipeline_entry()
            return _successful_embed(PhotocomParser.process_post(path))
        except PARSER_FALLBACK_EXCEPTIONS:
            pass

    share_source_path = None
    if Utils.is_share_path(path):
        share_source_path = path
        path, prefetched_response = Utils.resolve_share_link(path)

    return _handle_resolved_facebook_path(
        path,
        prefetched_response=prefetched_response,
        share_source_path=share_source_path,
    )


def _handle_resolved_facebook_path(
    path: str,
    prefetched_response: CffiResponse | None = None,
    share_source_path: str | None = None,
) -> str:

    try:
        parsed_post = _dispatch_post(path, prefetched_response)
    except ROUTE_FALLBACK_EXCEPTIONS as primary_error:
        if not _allow_route_upstream_fallback(primary_error):
            raise
        if share_source_path and share_source_path != path:
            try:
                _mark_parser_pipeline_entry()
                parsed_post = _invoke_parser(
                    JsonParser.process_post,
                    share_source_path,
                    prefetched_response,
                )
            except ROUTE_FALLBACK_EXCEPTIONS as share_error:
                if not _allow_route_upstream_fallback(share_error):
                    raise
                raise _prefer_parser_error(primary_error, share_error)
        else:
            raise
    return _successful_embed(parsed_post)


@app.route('/healthz')
def healthz():
    uptime_secs, request_count, error_count = service_metrics.snapshot()
    payload = {
        'status': 'ok',
        'uptime_secs': uptime_secs,
        'requests': request_count,
        'errors': error_count,
    }
    response.content_type = 'application/json'
    response.headers['Cache-Control'] = 'no-store'
    return json.dumps(payload)


@app.route('/<path:path>')
def index(path: str):
    dump_requested = bool(re.search(r'(?:^|/)dump/?$', path, re.IGNORECASE))
    if dump_requested:
        path = re.sub(r'(?:^|/)dump/?$', '', path, flags=re.IGNORECASE)
    path = path.rstrip('/')
    if request.query_string:
        path = f'{path}?{request.query_string}'
    original_path = path

    response.headers['Vary'] = 'User-Agent'
    response.headers['Cache-Control'] = 'no-store'
    if not crawleruseragents.is_crawler(
        request.headers.get('User-Agent', ''), case_sensitive=False
    ):
        response.status = 302
        response.headers['Location'] = f'{WWWFB}/{path}'
        return format_redirect_page(f'{WWWFB}/{path}')

    selected_response = None
    try:
        result, selected_response, parser_entered = _run_tracked_request(path)
        if not parser_entered:
            service_metrics.record_request()
    except Exception as exc:
        _record_scrape_error(exc)
        selected_response = getattr(exc, 'selected_response', None)
        if selected_response is None:
            selected_response = getattr(exc, 'upstream_response', None)
        reason = getattr(exc, 'reason_code', 'internal_error')
        logging.warning(
            'embed fallback type=%s reason=%s url=%s detail=%s',
            failed_post_type(original_path, selected_response, reason),
            reason,
            f'{WWWFB}/{original_path}',
            ' '.join(str(exc).split()),
        )
        if isinstance(exc, NoDataException):
            _send_restricted_page_report(original_path, exc, selected_response)
        result = format_error_message_embed(f'{WWWFB}/{original_path}')

    if dump_requested:
        _send_dump_report(path, selected_response)
        response.headers['Cache-Control'] = 'no-store'
    return result


@app.route('/favicon.ico')
def favicon():
    response.content_type = 'image/x-icon'
    return static_file('favicon.ico', root=str(ASSETS_DIR))


@app.route('/banner.png')
def banner():
    response.content_type = 'image/png'
    return static_file('banner.png', root=str(ASSETS_DIR))


@app.route('/')
def root():
    with (ASSETS_DIR / 'index.html').open(encoding='utf-8') as f:
        return f.read().replace('{|CREDIT|}', get_credit())


def log_to_logger(fn):
    @wraps(fn)
    def _log_to_logger(*argsz, **kwargs):
        actual_response = fn(*argsz, **kwargs)
        logging.info('%s %s %s %s', request.remote_addr, request.method, request.url, response.status)
        return actual_response

    return _log_to_logger


def main():
    global config

    parser = argparse.ArgumentParser(description='Facebook embed server')
    parser.add_argument('-c', '--config', type=str, help='config yaml file path')
    args = parser.parse_args()

    if args.config:
        if not os.path.isfile(args.config):
            logging.error(f'config file {args.config} not found or is not a file')
            exit(1)
        if not os.access(args.config, os.R_OK):
            logging.error(f'config file {args.config} not readable')
            exit(1)

        with open(args.config, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        for dk in default_config:
            if dk not in config:
                config[dk] = default_config[dk]
        for k in config:
            if k not in default_config or type(config[k]) != type(default_config[k]):
                logging.error(f'invalid config entry {k}')
                exit(1)
    else:
        config = default_config

    if config['timezone'] < -12 or config['timezone'] > 14:
        logging.critical('invalid timezone offset')
        exit(1)

    if sys.version_info.minor < 12:
        logging.error('python 3.12+ required, see https://docs.python.org/3.12/whatsnew/3.12.html#pep-701-syntactic-formalization-of-f-strings')
        exit(1)

    logging.info(f'listening on {config["host"]}:{config["port"]}')
    app.install(log_to_logger)
    app.run(host=config['host'], port=config['port'], quiet=True)


if __name__ == '__main__':
    main()
