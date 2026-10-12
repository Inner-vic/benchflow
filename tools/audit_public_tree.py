"""Scan tracked public files and all reachable commit blobs for publication risks."""
import ipaddress
import json
import re
import subprocess
from urllib.parse import urlsplit
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PATTERNS={
    "private_key":r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----",
    "access_token":r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|glpat-[A-Za-z0-9_-]{15,}|AKIA[A-Z0-9]{16})",
}
# The checker itself contains rejection patterns, not actual deployment information.
SELF=Path(__file__).relative_to(ROOT).as_posix()
ALLOWED_NETS=[ipaddress.ip_network(x) for x in ("192.0.2.0/24","198.51.100.0/24","203.0.113.0/24","127.0.0.0/8")]
IP_RE=re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
findings=[]
def check(name,data):
    try:text=data.decode("utf-8")
    except UnicodeDecodeError:
        if name.split(":",1)[-1]=="docs/images/workflow-demo.gif" and data[:6] in {b"GIF87a",b"GIF89a"}:
            return  # Generated and visually reviewed architecture illustration.
        findings.append({"file":name,"kind":"unreviewed_binary"});return
    if name.split(":",1)[-1]!=SELF:
        for kind,pattern in PATTERNS.items():
            if re.search(pattern,text):findings.append({"file":name,"kind":kind})
        for value in re.findall(r'https?://[^\s"\x27<>`]+',text):
            host=urlsplit(value).hostname
            # Public brand documentation reviewed for the architecture illustrations.
            if host and not (host.endswith('.example') or host in {'github.com','inner-vic.github.io','json-schema.org','www.w3.org','127.0.0.1','localhost','openai.com','claude.com','design.ubuntu.com'}):
                findings.append({"file":name,"kind":"unreviewed_endpoint"})
        for account in re.findall(r'/home/([A-Za-z0-9_.-]+)',text):
            if account not in {'bench-user','example-user','user','shared'}:
                findings.append({"file":name,"kind":"unreviewed_home_account"})
        for vehicle in re.findall(r'\b[A-HJ-NPR-Z0-9]{17}\b',text):
            if not vehicle.startswith('DEMO'):
                findings.append({"file":name,"kind":"possible_vehicle_identifier"})
        if re.search(r'SHA256:[A-Za-z0-9+/]{35,}',text):
            findings.append({"file":name,"kind":"fixed_host_fingerprint"})
    for value in IP_RE.findall(text):
        try:address=ipaddress.ip_address(value)
        except ValueError:continue
        if address.is_private and not any(address in net for net in ALLOWED_NETS) and value!="0.0.0.0":
            findings.append({"file":name,"kind":"non_example_private_address"})
files=subprocess.check_output(["git","ls-files","-z"],cwd=ROOT).decode().split("\0")
for name in filter(None,files):check(name,(ROOT/name).read_bytes())
commits=subprocess.check_output(["git","rev-list","--all"],cwd=ROOT).decode().splitlines()
blob_count=0
for commit in commits:
    entries=subprocess.check_output(["git","ls-tree","-r","-z",commit],cwd=ROOT).decode().split("\0")
    for entry in filter(None,entries):
        meta,name=entry.split("\t",1)
        if meta.split()[1]!="blob":continue
        check(commit[:8]+":"+name,subprocess.check_output(["git","cat-file","blob",meta.split()[2]],cwd=ROOT));blob_count+=1
print(json.dumps({"tracked_files":len(list(filter(None,files))),"reachable_commits":len(commits),"history_blobs_checked":blob_count,"findings":findings},ensure_ascii=False,indent=2))
raise SystemExit(bool(findings))
