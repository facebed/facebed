# facebed
A Facebook embed provider for Discord and other messaging apps.

[![2IrN8Ux.png](https://iili.io/2IrN8Ux.png)](https://freeimage.host/)


## For users:
Replace `www.facebook.com` in your Facebook URL to `facebed.com`.

Regex for Vencord settings:
- Find: `https://(www.)?facebook.com/(.*)`
- Replace with: `https://facebed.com/$2`

# For developers and maintainers

## System requirement:
[Python 3.14+](https://www.python.org/downloads/)

## Deployment
The [Dockerfile](/Dockerfile) can be used for deployment.

## Running locally
It's recommended to run `facebed` in a [virtual environment](https://docs.python.org/3/library/venv.html) if you don't want to pollute your
system-wide python installation.

Open a command prompt in the facebed repository.

Remember to install dependencies:
```bash
pip install -r requirements.txt
```
To start facebed:
```bash
python facebed.py
```

## Cookies (DO NOT USE)
Use the cookies exported using the extension Cookie Editor with the json format. Will warn 
maintainers if any cookies expired.
**DO NOT USE RIGHT NOW, WILL CAUSE A CHECKPOINT ON YOUR ACCOUNT**
