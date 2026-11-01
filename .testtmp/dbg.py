import os, traceback
os.environ.setdefault("DJANGO_SETTINGS_MODULE","config.settings")
import django; django.setup()
from pathlib import Path
from uploader.services.downloader import fetch_to_file, DownloadError
from uploader.services.netguard import ValidatedURL

class R:
    status=200
    headers={"content-type":"application/octet-stream"}
    def __init__(self,b): self.b=b; self.p=0
    def read(self,n):
        d=self.b[self.p:self.p+n]; self.p+=len(d); return d
    def close(self): pass

t=ValidatedURL(url="https://93.184.216.34/x.mp4",scheme="https",host="93.184.216.34",port=443,addresses=("93.184.216.34",))
dest=Path(".testtmp")/"manual.part"
dest.parent.mkdir(parents=True,exist_ok=True)
try:
    fetch_to_file(t,dest,max_bytes=10000,transport=lambda v: R(b"not a video at all"))
except Exception as e:
    print("TYPE:",type(e).__name__,"CODE:",getattr(e,"code",None),"MSG:",e)
    traceback.print_exc()
