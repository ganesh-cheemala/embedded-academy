#!/usr/bin/env python3
import os, sys
from pathlib import Path
errors=[]
root=Path(__file__).resolve().parents[1]
env=root/".env"
if not env.exists(): errors.append(".env file is missing")
required=["POSTGRES_PASSWORD","ACADEMY_SECRET","ACADEMY_ADMIN_PASSWORD","ACADEMY_BASE_URL","ACADEMY_TRUSTED_HOSTS"]
vals={}
if env.exists():
    for line in env.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k,v=line.split("=",1); vals[k.strip()]=v.strip()
for key in required:
    if not vals.get(key): errors.append(f"{key} is not set")
for key in ("POSTGRES_PASSWORD","ACADEMY_SECRET","ACADEMY_ADMIN_PASSWORD"):
    val=vals.get(key,"")
    if val.lower().startswith("replace-with") or val in {"change-this-password","admin123","dev-only-change-me"}: errors.append(f"{key} still contains a placeholder/default")
if vals.get("ACADEMY_ENV","production").lower()=="production" and not vals.get("ACADEMY_BASE_URL","").startswith("https://"): errors.append("ACADEMY_BASE_URL should use https:// in production")
print("KCI Academy launch self-check")
if errors:
    print("\n".join(f"ERROR: {e}" for e in errors)); sys.exit(1)
print("OK: required production settings are present.")
