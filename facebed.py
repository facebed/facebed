import argparse
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
from typing import Self, Callable
from urllib.parse import quote as _quote_
from urllib.parse import urlparse

import crawleruseragents
import requests as rq
import stealth_requests as requests
import yaml
from bottle import Bottle, request, response, static_file
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook, DiscordEmbed
from yattag import indent

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

WWWFB = 'https://www.facebook.com'
TZ_OFFSET: int = 0
ALLOW_UPDATE = True
logging.basicConfig(format='[%(levelname)s] [%(asctime)s] %(msg)s', level=logging.INFO)


def quote(s: str) -> str:
    return "".join([
        _quote_(char) if char in r"<>\"'#%{}[]|\\^~`" else char
        for char in s
    ])

def get_credit() -> str:
    return 'facebed by pi.kt'


class Utils:
    @staticmethod
    def resolve_share_link(path: str) -> str:
        url = f'{WWWFB}/{path}'
        logging.info(f'Resolving share link: {url}')
        head_request = rq.head(url, headers=JsonParser.get_headers(), allow_redirects=True)
        logging.info(f'Resolved to: {head_request.url}')
        if head_request.url.startswith(('https://www.facebook.com/share', 'https://web.facebook.com/share')):
            logging.warning('Share link still redirects to share page')
            return ''
        
        for base in ['https://www.facebook.com', 'https://web.facebook.com']:
            if head_request.url.startswith(f'{base}/'):
                return head_request.url.removeprefix(f'{base}/')
        return head_request.url

    @staticmethod
    def prettify(txt: str) -> str:
        return indent(txt, indentation ='    ', newline = '\n', indent_text = True)

    @staticmethod
    def warn(msg: str = None, file_content: bytes = None, filename: str = None, embed: DiscordEmbed = None):
        def worker():
            wh = config['notifier_webhook']
            if not wh or not wh.startswith('https://discord.com/api/webhooks/'):
                return
            try:
                webhook = DiscordWebhook(url=config['notifier_webhook'], content=msg)
                if embed:
                    webhook.add_embed(embed)
                if file_content and filename:
                    webhook.add_file(file=file_content, filename=filename)
                webhook.execute()
            except Exception:
                logging.warning(f'failed to warn about "{msg or embed}"')

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def d(o, no):
        with open(f'test{no}.json', 'w', encoding='utf-8') as f:
            f.write(json.dumps(o, ensure_ascii=False, indent=2))

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
    def format_reactions_str(likes: str, cmts: str, shares: str) -> str:
        likes_str = f'❤️ {likes}' if likes != 'null' else ''
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

    @staticmethod
    def last(obj: dict, key: str) -> dict:
        return Jq.iterate(obj, key)[-1]


class Cookies:
    def __init__(self, fn: str):
        self.cookies: list = []

        if not os.path.isfile(fn):
            logging.warning('cookies.json not found, non incognito-viewable posts will NOT work')
            return

        with open(fn) as f:
            self.cookies = json.load(f)
            logging.info(f'loaded {len(self.cookies)} cookies from {fn}')
        self.get_cookies()

    # noinspection PyMethodMayBeStatic
    def is_valid_cookie(self, entry: dict) -> bool:
        return int(entry.get('expirationDate', 2**31)) > time.time()

    def get_cookies(self) -> dict[str, str]:
        if any([not self.is_valid_cookie(cookie) for cookie in self.cookies]):
            Utils.warn('@everyone cookies expired')
            return {}

        return {k['name']: k['value'] for k in self.cookies}


acc = Cookies('cookies.json')


class Story:
    author_name: str
    text: str
    image_links: list[str]
    video_links: list[str]
    url: str

    author_id: int
    attached_story: Self

    def __init__(self, story_json: dict):
        self.author_name = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'name' in story_json['actors'][0]:
            self.author_name = story_json['actors'][0]['name']
        else:
            self.author_name = Jq.first(story_json, 'name')
            if not self.author_name or not isinstance(self.author_name, str):
                self.author_name = Jq.first(story_json, 'localized_name') or ''

        self.text = ''
        if 'message' in story_json and story_json['message'] and 'text' in story_json['message']:
            self.text = story_json['message']['text']
        else:
            self.text = Jq.first(story_json, 'text') or ''

        self.image_links = self.get_image_links_post_json(story_json)
        self.video_links = self.get_video_links(story_json)
        
        self.url = story_json.get('wwwURL') or Jq.first(story_json, 'url') or ''
        if not isinstance(self.url, str):
            self.url = ''

        self.author_id = ''
        if 'actors' in story_json and story_json['actors'] and isinstance(story_json['actors'], list) and len(story_json['actors']) > 0 and 'id' in story_json['actors'][0]:
            self.author_id = story_json['actors'][0]['id']
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
        all_attachments = Jq.all(post_json, 'attachment')
        for attachment_set in all_attachments:
            if any([k.endswith('subattachments') for k in attachment_set]):
                subsets = [v for k, v in attachment_set.items() if k.endswith('subattachments') and 'nodes' in v]
                max_imgage_count = len(max(subsets, key=lambda it: len(it['nodes']))['nodes'])
                subsets = [subset for subset in subsets if
                           len(subset['nodes']) == max_imgage_count and Jq.all(subset, 'viewer_image')]
                images = [x['uri'] for x in Jq.all(subsets[0], 'viewer_image')]
                if images:
                    return images
            elif 'media' in attachment_set and "'__typename': 'Sticker'" not in str(attachment_set):
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


def banned(url: str) -> ParsedPost:
    Utils.warn(f'banned embed attempted "{url}"')
    return ParsedPost('Banned', 'This user is banned by the operators of this embed server',
                      [], 'https://banned.facebook.com', -1,
                      'null', 'null', 'null', [])


class FacebedException(Exception):
    pass


class NoDataException(FacebedException):
    pass


class ParseException(FacebedException):
    def __init__(self, message: str, html: str = '', url: str = ''):
        super().__init__(message)
        self.html = html
        self.url = url


class JsonParser:
    @staticmethod
    def get_headers() -> dict:
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'no-cache',
            'pragma': 'no-cache',
            'priority': 'u=0, i',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
        }

        return headers

    @staticmethod
    def get_json_blocks(html_parser: BeautifulSoup, sort=True) -> list[dict]:
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
    def probe_page_type(html_parser: BeautifulSoup) -> str:
        blocks = JsonParser.get_json_blocks(html_parser, sort=False)
        
        has_post_data = False
        for bloc in blocks:
            if Jq.has(bloc, 'i18n_reaction_count') or Jq.has(bloc, 'short_form_video_context'):
                has_post_data = True
                break
        
        if has_post_data:
            return 'has_data'
            
        canonical = html_parser.find('link', attrs={'rel': 'canonical'})
        if canonical and re.search(r'/login\b', canonical.get('href', '')):
            return 'login_wall'
        
        return 'no_data'

    @staticmethod
    def check_page_or_raise(html_parser: BeautifulSoup, post_path: str):
        page_type = JsonParser.probe_page_type(html_parser)
        if page_type in ('login_wall', 'no_data'):
            raise NoDataException(f'Facebook served a login wall or empty page for {post_path} - content requires authentication')

    @staticmethod
    @contextmanager
    def fetch_page(post_path: str, use_cookies=True):
        url = JsonParser.ensure_full_url(post_path)
        kw = {'headers': JsonParser.get_headers()}
        if use_cookies:
            cookies = acc.get_cookies()
            if cookies:
                kw['cookies'] = cookies
        http_response = requests.get(url, **kw)
        raw_html = http_response.text
        html_parser = BeautifulSoup(raw_html, 'html.parser')
        JsonParser.check_page_or_raise(html_parser, post_path)
        try:
            yield html_parser
        except ParseException as e:
            if not e.html:
                e.html = raw_html
                e.url = url
            raise

    @staticmethod
    def get_post_json(html_parser: BeautifulSoup) -> dict:
        candidate_blocks = []
        for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
            if Jq.has(bloc, 'i18n_reaction_count') or Jq.has(bloc, 'short_form_video_context'):
                candidate_blocks.append(bloc)

        if not candidate_blocks:
            for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
                if 'video' in str(bloc) and 'short_form_video_context' in str(bloc):
                    candidate_blocks.append(bloc)
                    
        if not candidate_blocks:
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
            return score

        candidate_blocks.sort(key=score_block, reverse=True)
        return candidate_blocks[0]

    @staticmethod
    def get_group_name(html_parser: BeautifulSoup) -> str:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'group_member_profiles', 'formatted_count_text'):
                for group_object in Jq.all(bloc, 'group'):
                    if 'name' in group_object:
                        return group_object['name']
        return ''

    @staticmethod
    def get_interaction_counts(post_json: dict) -> tuple[str, str, str]:
        assert post_json
        
        post_feedback = Jq.first(post_json, 'comet_ufi_summary_and_actions_renderer')
        if post_feedback and isinstance(post_feedback, dict) and 'feedback' in post_feedback:
            fb = post_feedback['feedback']
            reactions = fb.get('i18n_reaction_count') or Jq.first(fb, 'i18n_reaction_count') or '0'
            shares = fb.get('i18n_share_count') or Jq.first(fb, 'i18n_share_count') or '0'
            
            comments = '0'
            if 'comment_rendering_instance' in fb and isinstance(fb['comment_rendering_instance'], dict):
                comments_node = fb['comment_rendering_instance'].get('comments')
                if comments_node and isinstance(comments_node, dict):
                    comments = str(comments_node.get('total_count', '0'))
            if comments == '0':
                comments = str(fb.get('total_comment_count') or Jq.first(fb, 'total_comment_count') or '0')
                
            return str(reactions), str(comments), str(shares)

        fb = Jq.first(post_json, 'feedback')
        if fb and isinstance(fb, dict):
            reactions = fb.get('i18n_reaction_count') or Jq.first(fb, 'i18n_reaction_count') or '0'
            shares = fb.get('i18n_share_count') or fb.get('share_count') or Jq.first(fb, 'i18n_share_count') or Jq.first(fb, 'share_count') or '0'
            comments = fb.get('total_comment_count') or Jq.first(fb, 'total_comment_count')
            if not comments:
                if 'comment_rendering_instance' in fb and isinstance(fb['comment_rendering_instance'], dict):
                    comments_node = fb['comment_rendering_instance'].get('comments')
                    if comments_node and isinstance(comments_node, dict):
                        comments = comments_node.get('total_count')
            if not comments:
                comments = '0'
            return str(reactions), str(comments), str(shares)

        reactions = Jq.first(post_json, 'i18n_reaction_count') or '0'
        shares = Jq.first(post_json, 'i18n_share_count') or Jq.first(post_json, 'share_count') or '0'
        comments = Jq.first(post_json, 'total_comment_count') or '0'
        
        return str(reactions), str(comments), str(shares)

    @staticmethod
    def get_root_node(post_json: dict) -> dict:
        def work_normal_post() -> dict:
            data_blob = Jq.first(post_json, 'data')
            if not isinstance(data_blob, dict):
                short_form = Jq.first(post_json, 'short_form_video_context')
                if short_form:
                    return {'creation_story': short_form}
                return {}
            if 'comet_ufi_summary_and_actions_renderer' in data_blob:   # single photo
                return data_blob
            elif 'node_v2' in data_blob and isinstance(data_blob['node_v2'], dict) and 'comet_sections' in data_blob['node_v2']:
                return data_blob['node_v2']['comet_sections']
            elif 'node' in data_blob and isinstance(data_blob['node'], dict) and 'comet_sections' in data_blob['node']:
                return data_blob['node']['comet_sections']
            short_form = Jq.first(data_blob, 'short_form_video_context')
            if short_form:
                return {'creation_story': short_form}
            return {}

        def work_group_post() -> dict:
            hoisted_feed = Jq.first(post_json, 'group_hoisted_feed')
            if isinstance(hoisted_feed, dict) and 'comet_sections' in hoisted_feed:
                return hoisted_feed['comet_sections']
            
            data_blob = Jq.first(post_json, 'data')
            if isinstance(data_blob, dict):
                group = data_blob.get('group')
                if isinstance(group, dict):
                    comet_sections = Jq.first(group, 'comet_sections')
                    if comet_sections:
                        return comet_sections
            return {}

        methods: list[Callable[[], dict]] = [work_normal_post, work_group_post]

        for method in methods:
            try:
                ret = method()
                if ret:
                    return ret
                else:
                    continue
            except (StopIteration, KeyError):
                continue

        data_blob = Jq.first(post_json, 'data')
        if isinstance(data_blob, dict) and 'creation_story' in data_blob and 'feedback' in data_blob:
            return data_blob

        raise ParseException('Cannot process post')

    @staticmethod
    def ensure_full_url(u: str) -> str:
        if u.startswith(WWWFB):
            return u
        else:
            return f'{WWWFB}/{u.removeprefix("/")}'

    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        with JsonParser.fetch_page(post_path) as html_parser:
            post_json = JsonParser.get_root_node(JsonParser.get_post_json(html_parser))
            likes, cmts, shares = JsonParser.get_interaction_counts(post_json)
            
            # robust date finding
            post_date = -1
            t = Jq.first(post_json, 'creation_time') or Jq.first(post_json, 'created_time')
            if t:
                try:
                    post_date = int(t)
                except (ValueError, TypeError):
                    pass
            if post_date == -1:
                for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
                    t = Jq.first(bloc, 'creation_time') or Jq.first(bloc, 'created_time')
                    if t:
                        try:
                            post_date = int(t)
                            break
                        except (ValueError, TypeError):
                            pass

            story_dict = post_json
            if 'content' in post_json and isinstance(post_json['content'], dict) and 'story' in post_json['content']:
                story_dict = post_json['content']['story']
            elif 'creation_story' in post_json:
                story_dict = post_json['creation_story']
                if 'owner' in post_json and ('actors' not in story_dict or not story_dict['actors']):
                    story_dict['actors'] = [post_json['owner']]

            story = Story(story_dict)
            post_url = story.url or JsonParser.ensure_full_url(post_path)
            post_content = story.get_text()
            post_group_name = JsonParser.get_group_name(html_parser)
            post_author_name = story.author_name
            link_header = f'{post_author_name}' + (f' • {post_group_name}' if post_group_name else '')

            if story.author_id in config['banned_users']:
                return banned(post_url)

            # TODO: support normal /watch here
            return ParsedPost(link_header, post_content.strip(), story.image_links, post_url, post_date,
                              likes, cmts, shares, story.video_links)


class SinglePhotoParser:
    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'message_preferred_body', 'container_story'):
                return Jq.first(bloc, 'data')
        raise ParseException('Cannot process post (cn)')

    @staticmethod
    def get_interactions_node(html_parser: BeautifulSoup) -> dict:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'comet_ufi_summary_and_actions_renderer'):
                return bloc
        raise ParseException('Cannot process post (in)')

    @staticmethod
    def get_single_image(html_parser: BeautifulSoup) -> str:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'prefetch_uris_v2'):
                return str(Jq.first(bloc, 'prefetch_uris_v2')[0]['uri'])
        raise ParseException('cannot find single image')

    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        with JsonParser.fetch_page(post_path) as html_parser:
            content_node = SinglePhotoParser.get_content_node(html_parser)
            interaction_node = SinglePhotoParser.get_interactions_node(html_parser)

            post_text = content_node['message']['text'] if content_node['message'] and 'text' in content_node['message'] else ''
            post_author = content_node['owner']['name']
            post_date = content_node['created_time']
            likes, cmts, shares = JsonParser.get_interaction_counts(interaction_node)
            image_url = SinglePhotoParser.get_single_image(html_parser)

            return ParsedPost(post_author, post_text.strip(), [image_url], JsonParser.ensure_full_url(post_path),
                              post_date, likes, cmts, shares, [])


class PhotocomParser:
    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'attached_comment') and not Jq.has(bloc, 'unified_reactors'):
                return Jq.first(bloc, 'result')
        raise ParseException('Cannot process photocom (cn)')

    @staticmethod
    def get_reaction_count(html_parser: BeautifulSoup) -> int:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'attached_comment', 'unified_reactors'):
                return Jq.first(bloc, 'unified_reactors')['count']
        raise ParseException('Cannot process photocom (rc)')

    @staticmethod
    def get_attached_image_and_url(html_parser: BeautifulSoup) -> tuple[str, str]:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'attached_comment', 'unified_reactors'):
                cur = Jq.first(bloc, 'currMedia')
                return str(cur['image']['uri']), str(cur['attached_comment']['feedback']['url'])
        raise ParseException('Cannot process photocom (iau)')

    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        with JsonParser.fetch_page(post_path) as html_parser:
            content_node = PhotocomParser.get_content_node(html_parser)
            body = content_node['data']['attached_comment']['preferred_body']

            op_name = content_node['data']['owner']['name'] + ' (💬)'
            post_text = '' if body is None else body['text']
            post_time = content_node['data']['created_time']
            post_image, post_url = PhotocomParser.get_attached_image_and_url(html_parser)
            reaction_count = PhotocomParser.get_reaction_count(html_parser)

            return ParsedPost(op_name, post_text, [post_image], post_url, post_time, Utils.human_format(reaction_count), 'null', 'null', [])


class ReelsParser:
    @staticmethod
    def get_video_link(html_parser: BeautifulSoup|None, user_node: dict = None) -> str:
        def work_node(node: dict) -> str:
            video_node = Jq.first(node, 'videoDeliveryLegacyFields')
            for key in ['browser_native_hd_url', 'browser_native_sd_url']:
                try:
                    video_link = Jq.first(video_node, key)
                    if not video_link:
                        continue
                    return str(video_link)
                except StopIteration:
                    pass
            raise ParseException('Invalid reels link (vn)')

        if user_node:
            return work_node(user_node)

        # randomly breaks if sorted
        for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
            if Jq.has(bloc, 'browser_native_hd_url') or Jq.has(bloc, 'browser_native_sd_url'):
                return work_node(bloc)

        raise ParseException('Invalid reels link (vn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc, 'browser_native_sd_url', 'creation_story'):
                node = Jq.first(bloc, 'creation_story')
                if node is not None:
                    return node
            if Jq.has(bloc, 'short_form_video_context'):
                node = Jq.first(bloc, 'short_form_video_context')
                if node is not None:
                    return node
        raise ParseException('Invalid reels link (cn)')

    @staticmethod
    def get_reaction_counts(html_parser: BeautifulSoup, is_ig: bool, video_id: str) -> tuple[str, str, str]:
        blocks: list[dict] = []
        for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
            if Jq.has(bloc, 'unified_reactors'):
                if any([vid == video_id for vid in Jq.all(bloc, 'id')]):
                    blocks.append(bloc)

        if len(blocks) == 0:
            raise ParseException('Cannot process post (cn)')

        # assuming the last one contains ig info
        bloc = blocks[0]
        first_fb = Jq.first(bloc, 'feedback')
        last_fb = Jq.last(bloc, 'feedback')

        if 'cross_universe_feedback_info' in str(first_fb):
            first_fb, last_fb = last_fb, first_fb

        ig_cmts = last_fb['cross_universe_feedback_info']['ig_comment_count']
        likes = first_fb['unified_reactors']['count']
        cmts = ig_cmts if is_ig else last_fb['total_comment_count']
        shares = last_fb['share_count_reduced'] # TODO: investigate why it's "reduced"

        return Utils.human_format(likes), Utils.human_format(cmts), Utils.human_format(shares)


    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        with JsonParser.fetch_page(post_path, use_cookies=True) as html_parser:
            content_node = ReelsParser.get_content_node(html_parser)

            video_link = ReelsParser.get_video_link(html_parser)
            video_id = content_node.get('id') or content_node.get('video', {}).get('id')
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
                for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
                    post_date = Jq.first(bloc, 'creation_time') or Jq.first(bloc, 'created_time')
                    if post_date:
                        try:
                            post_date = int(post_date)
                            break
                        except (ValueError, TypeError):
                            post_date = None
            if not post_date:
                post_date = -1

            post_text = '' if content_node.get('message') is None else content_node['message']['text']

            likes, cmts, shares = ReelsParser.get_reaction_counts(html_parser, is_ig, video_id)

            if owner_info['id'] in config['banned_users']:
                return banned(post_url)

            return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


class VideoWatchParser:
    # excluding group post video since they are handled by jsonparser
    @staticmethod
    def get_op_name(html_parser: BeautifulSoup) -> str:
        for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
            if Jq.has(bloc, 'is_additional_profile_plus'):
                return Jq.first(bloc, 'owner')['name']
        for bloc in JsonParser.get_json_blocks(html_parser, sort=False):
            owner = Jq.first(bloc, 'owner')
            if isinstance(owner, dict) and 'name' in owner:
                return owner['name']
        raise ParseException('Invalid watch link (opn)')

    @staticmethod
    def get_content_node(html_parser: BeautifulSoup) -> dict:
        for bloc in JsonParser.get_json_blocks(html_parser):
            if Jq.has(bloc,'comment_rendering_instance', 'video_view_count_renderer'):
                return Jq.first(bloc, 'result')['data']
        canonical = html_parser.find('link', attrs={'rel': 'canonical'})
        if canonical and re.match(r'https?://[^/]+/watch/?$', canonical.get('href', '')):
            raise NoDataException('Facebook served generic watch feed instead of specific video')
        raise ParseException('Invalid watch link (cn)')

    @staticmethod
    def get_date(html_parser: BeautifulSoup) -> int:
        for json_block in JsonParser.get_json_blocks(html_parser):
            if Jq.has(json_block, 'creation_time'):
                #   noinspection PyTypeChecker
                return int(Jq.first(json_block, 'creation_time'))
        raise ParseException('cannot find date')

    @staticmethod
    def process_post(post_path: str) -> ParsedPost:
        with JsonParser.fetch_page(post_path, use_cookies=True) as html_parser:
            content_node = VideoWatchParser.get_content_node(html_parser)

            video_link = ReelsParser.get_video_link(html_parser)

            post_url = JsonParser.ensure_full_url(post_path)
            op_name = VideoWatchParser.get_op_name(html_parser)
            post_text = content_node['title']['text'] if (content_node.get('title') and isinstance(content_node['title'], dict) and content_node['title'].get('text')) else ''
            if not post_text:
                msg = Jq.first(content_node, 'message')
                if isinstance(msg, dict):
                    post_text = msg.get('text', '')

            likes = Utils.human_format(content_node['feedback']['reaction_count']['count'])
            shares = 'null'
            cmts = Utils.human_format(content_node['feedback']['total_comment_count'])
            post_date = VideoWatchParser.get_date(html_parser)

            return ParsedPost(op_name, post_text, [], post_url, post_date, likes, cmts, shares, [video_link])


def format_error_message_embed(original_url: str) -> str:
    return Utils.prettify(f'''<!DOCTYPE html>
<html lang="">
<head>
<meta charset="UTF-8" />
    <meta name="theme-color" content="#2c3048f" />
    <meta property="og:title" content="Log in or sign up to view"/>
    <meta property="og:description" content="See posts, photos and more on Facebook."/>
    <meta http-equiv="refresh" content="0;url={quote(original_url)}"/>
</head>
</html>''')


def is_facebook_url(url: str) -> bool:
    wwwfb = f'{WWWFB}/'
    username_pattern = '[a-zA-Z0-9-._]*'  # also covers /watch
    full_url = f'{wwwfb}{url}'
    parsed_url = urlparse(full_url)

    is_group_post = re.match(f'^/groups/{username_pattern}', parsed_url.path)
    is_permalink = parsed_url.path.startswith('/permalink.php')
    is_story = parsed_url.path.startswith('/story.php')
    is_post = re.match(f'/{username_pattern}/posts', parsed_url.path)
    is_photo = parsed_url.path.startswith('/photo')

    return is_permalink or is_post or is_story or is_photo or is_group_post


def format_reel_post_embed(post: ParsedPost) -> str:
    def get_video_meta_tag(link: str) -> str:
        return '\n'.join([
            f'<meta property="twitter:player:stream" content="{link}"/>',
            f'<meta property="og:video" content="{link}"/>'
            f'<meta property="og:video:secure_url" content="{link}"/>'
        ])

    video_meta_tags = '\n'.join([get_video_meta_tag(vu) for vu in post.video_links])
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)
    post_date = Utils.timestamp_to_str(post.date)
    color = '#0866ff'

    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{get_credit()}\n{post_date}\n{reaction_str}"/>
            <meta property="og:url" content="{quote(post.url)}"/>
            <meta property="og:video:type" content="video/mp4"/>
            <meta property="twitter:player:stream:content_type" content="video/mp4"/>

            {video_meta_tags}

            <link rel="canonical" href="{quote(post.url)}"/>
            <meta http-equiv="refresh" content="0;url={quote(post.url)}"/>
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
    image_meta_tags = '\n'.join([f'<meta property="og:image" content="{iu}"/>' for iu in image_links])
    post_date = Utils.timestamp_to_str(post.date)
    reaction_str = Utils.format_reactions_str(post.likes, post.comments, post.shares)

    # TODO: organize and duplicate the neccessary tags
    return Utils.prettify(f'''<!DOCTYPE html>
        <html lang="">
        <head>
            <title>{get_credit()}</title>
            <meta charset="UTF-8"/>
            <meta property="og:title" content="{escape(post.author_name)}"/>
            <meta property="og:description" content="{escape(post.text[:1024])}"/>
            <meta property="og:site_name" content="{get_credit()}\n{post_date}\n{reaction_str}{image_counter}"/>
            <meta property="og:url" content="{quote(post.url)}"/>
            {image_meta_tags}
            <link rel="canonical" href="{quote(post.url)}"/>
            <meta http-equiv="refresh" content="0;url={quote(post.url)}"/>
            <meta name="twitter:card" content="summary_large_image"/>
            <meta name="theme-color" content="#0866ff"/>
        </head>
        </html>''')


def format_redirect_page(url: str) -> str:
    return Utils.prettify(f'''<!DOCTYPE HTML>
<html lang="en-US">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="refresh" content="0; url={quote(url)}">
        <script type="text/javascript">
            window.location.href = "{escape(url)}"
        </script>
        <title>redirecting...</title>
    </head>
    <body>
    </body>
</html>''')


def process_post(post_path: str) -> str:
    post_path = post_path.removeprefix(WWWFB).removeprefix('/')
    parsed_post = JsonParser.process_post(post_path)
    if type(parsed_post) == ParsedPost:
        return format_full_post_embed(parsed_post)
    return format_error_message_embed(f'{WWWFB}/{post_path}')


def process_single_photo(post_path: str) -> str:
    parsed_post = SinglePhotoParser.process_post(post_path)
    if type(parsed_post) == ParsedPost:
        return format_full_post_embed(parsed_post)
    return format_error_message_embed(f'{WWWFB}/{post_path}')


@app.route('/<path:path>')
def index(path: str):
    original_path = path
    if request.query_string:
        original_path = f"{original_path}?{request.query_string}"
        path += f'?{request.query_string}'

    # /dump diagnostic: strip the suffix, fetch the page, and send raw HTML as an error report
    if re.match(r'^(.*)/dump/?$', path, re.IGNORECASE):
        dump_path = re.sub(r'/dump/?$', '', path, flags=re.IGNORECASE)
        if not dump_path:
            dump_path = ''
        try:
            url = JsonParser.ensure_full_url(dump_path)
            kw = {'headers': JsonParser.get_headers()}
            if acc.get_cookies():
                kw['cookies'] = acc.get_cookies()
            http_response = requests.get(url, **kw)
            raw_html = http_response.text

            filename = re.sub(r'[^a-zA-Z0-9]', '_', dump_path)[:80] + '_dump.html'
            display_path = '/' + dump_path.lstrip('/')

            embed = DiscordEmbed(
                title="manual dump report",
                description=f"🔗 [`{display_path}`]({url})\n📋 Manual dump requested by user",
                color="3498DB"
            )
            embed.add_embed_field(name="Attached Payload", value=f"`{filename}`", inline=True)
            embed.add_embed_field(name="Response Size", value=f"{len(raw_html)} chars", inline=True)
            Utils.warn(file_content=raw_html.encode('utf-8'), filename=filename, embed=embed)

            logging.info(f'Dump report sent for /{dump_path}')
        except Exception:
            logging.error(f'Failed to generate dump for /{dump_path}:\n{traceback.format_exc()}')
        
        return process_post(dump_path)

    # processing image in comment
    # needs priority because this returns a different link than what the user gave it
    if 'type' in request.query.dict and '3' in request.query.dict['type']:
        try:
            return format_full_post_embed(PhotocomParser.process_post(path))
        except Exception:
            pass

    if not crawleruseragents.is_crawler(request.headers.get('User-Agent', '')):
        response.status = 301
        response.headers['Location'] = f'{WWWFB}/{path}'
        return format_redirect_page(f'{WWWFB}/{path}')

    try:
        if re.match('^(/)?share/v/.*', path):
            path = Utils.resolve_share_link(path)
            if not path:
                return format_error_message_embed(f'{WWWFB}/{path}')

        if re.match('^(/)?share/([pr]/)?[a-zA-Z0-9-._]*(/)?', path):
            path = Utils.resolve_share_link(path)
            if not path:
                return format_error_message_embed(f'{WWWFB}/{path}')

        search = re.search(r'/videos/(\d+).*', path)
        if search:
            video_id = search.group(1)
            path = f'reel/{video_id}'

        if re.match(f'^/?reel/[0-9]+', path):
            return format_reel_post_embed(ReelsParser.process_post(path))

        if re.match('^/*photo(\\.php)*/*$', urlparse(path).path):
            return process_single_photo(path)

        if re.match('^/*watch', urlparse(path).path):
            return format_reel_post_embed(VideoWatchParser.process_post(path))

        if is_facebook_url(path):
            return process_post(path)
        else:
            return format_error_message_embed('https://git.facebed.com')


    except NoDataException:
        logging.info(f'No data available for /{path} (login wall / restricted content)')
        return format_error_message_embed(f'{WWWFB}/{path}')
    except ParseException as e:
        logging.error(f'Parser bug for /{path}:\n{traceback.format_exc()}')
        page_url = e.url or f'{WWWFB}/{original_path}'
        filename = re.sub(r'[^a-zA-Z0-9]', '_', path)[:80] + '.html' if e.html else None
        
        display_path = '/' + original_path.lstrip('/')
        desc = f"🔗 [`{display_path}`]({page_url})\n🚩 {e}"
        if filename:
            desc += " and attached file"

        embed = DiscordEmbed(
            title="embed failure",
            description=desc,
            color="FF0000"
        )
        if filename:
            embed.add_embed_field(name="Attached Payload", value=f"`{filename}`", inline=True)

        if e.html:
            Utils.warn(file_content=e.html.encode('utf-8'), filename=filename, embed=embed)
        else:
            Utils.warn(embed=embed)
        return format_error_message_embed(f'{WWWFB}/{path}')
    except FacebedException:
        logging.warning(f'Unclassified FacebedException for /{path}:\n{traceback.format_exc()}')
        return format_error_message_embed(f'{WWWFB}/{path}')
    except Exception:
        logging.error(f'Unexpected error for /{path}:\n{traceback.format_exc()}')
        return format_error_message_embed(f'{WWWFB}/{path}')


@app.route('/favicon.ico')
def favicon():
    response.content_type = 'image/x-icon'
    return static_file('favicon.ico', root='./assets')


@app.route('/banner.png')
def banner():
    response.content_type = 'image/png'
    return static_file('banner.png', root='./assets')


@app.route('/')
def root():
    with open('assets/index.html', encoding='utf-8') as f:
        return f.read().replace('{|CREDIT|}', get_credit())


def log_to_logger(fn):
    @wraps(fn)
    def _log_to_logger(*argsz, **kwargs):
        actual_response = fn(*argsz, **kwargs)
        title = 'unknown'
        if isinstance(actual_response, str):
            error_match = re.search(r'content="Log in or sign up to view \[(.*)\]"', actual_response)
            if error_match:
                title = f'Error: {error_match.group(1)}'
            else:
                title_match = re.search(r'content="([^"]*)"', actual_response)
                if title_match:
                    title = title_match.group(1)
        logging.info('%s %s %s %s %s' % (request.remote_addr, request.method, request.url, response.status, title))
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
