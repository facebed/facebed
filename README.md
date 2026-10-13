# facebed
A Facebook embed provider for Discord and other messaging apps.

[![2IrN8Ux.png](https://iili.io/2IrN8Ux.png)](https://freeimage.host/)


## For users:
Replace `www.facebook.com` in your Facebook URL to `facebed.com`.
<details>
<summary>Vencord settings</summary>

Using regex:
- Find: `https://(www.)?facebook.com/(.*)`
- Replace: `https://facebed.com/$2`

</details>

# For developers and maintainers

## Deployment

1. Install `python>=3.12` with `pip` and `virtualenv`
2. Clone this repository
3. Copy `start.example.py` to `start.py` and edit the `PYTHON` variable to point to your venv or 
system installation (not recommended)
4. Copy `user_config.example.py` to `user_config.py` and edit the self-explanatory options
5. Start the `start.py` script

## Start command and virtual environment
I recommend running `facebed` in a virtual environment if you don't want to pollute your
system-wide python installation.

Copy `start.example.py` to `start.py` and edit the `PYTHON` variable to the full path of
your python interpreter. Two examples are provided for running `facebed` in a `venv` virtual environment on
Windows and Linux. After that, you can start `facebed` by running `python start.py` using your 
system's python since that script doesn't have any external dependencies.
