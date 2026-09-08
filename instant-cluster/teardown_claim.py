#!/usr/bin/env python3
"""SAFE teardown (proven order 2026-07-22): map-UUID-scoped ONLY, never name patterns.
Order matters: power OFF first (live kubelet re-attaches VGs, delete rejected otherwise),
then detach EVERY VG in the VM spec (CSI adds VGs beyond the restored pairs), then delete
VM, then delete the VGs. Usage: teardown_claim.py <claim-map.json> <claim-name>"""
import json,subprocess,os,sys,time,uuid as uuidlib
U,P=(os.environ.get("NKP_NUTANIX_USER") or os.environ["NUTANIX_USER"]), (os.environ.get("NKP_NUTANIX_PASSWORD") or os.environ["NUTANIX_PASSWORD"])
PC=(os.environ.get("NKP_PC_URL") or sys.exit("NKP_PC_URL is required (Prism Central URL, e.g. https://pc.example.com:9440)"))
V3=PC+"/api/nutanix/v3"; VOL=PC+"/api/volumes/v4.0/config/volume-groups"
mapf,claim=sys.argv[1],sys.argv[2]
M=json.load(open(mapf)); assert M["name"]==claim, "map %s is for %s, not %s"%(mapf,M["name"],claim)
uuids=[c["clone_uuid"] for c in M["cps"]]
if M.get("workers"):
    uuids += [w["clone_uuid"] for w in M["workers"]]
elif M.get("worker"):
    uuids.append(M["worker"]["clone_uuid"])
def api(m,u,b=None):
    a=["curl","-sk","--max-time","45","-u","%s:%s"%(U,P),"-X",m,u,"-H","Content-Type: application/json","-H","NTNX-Request-Id: "+str(uuidlib.uuid4())]
    if b is not None: a+=["-d",json.dumps(b)]
    return subprocess.run(a,capture_output=True,text=True).stdout
def vm_get(vmu):
    """Distinguish 'VM is really gone' from 'the API call failed'.

    BUG THIS FIXES (2026-07-26): the old code did `json.loads(api(...) or "{}")` and treated a
    missing "status" key as "already gone". During a network/PC outage every GET returned an empty
    string, so teardown declared all 5 VMs gone, deleted NOTHING, and printed "teardown done".
    A full 5-VM cluster (qa3-bcm1) stayed powered ON and later fought a new claim over its volume
    attachments. A destructive tool must NEVER fail open on its own verification step: a 404 means
    gone, anything else means unknown -> retry, then refuse.
    """
    for attempt in range(4):
        raw=api("GET",V3+"/vms/"+vmu)
        if raw:
            try:
                d=json.loads(raw)
            except Exception:
                time.sleep(5); continue
            if "status" in d: return d, "present"
            # a real 404 from PC carries an explicit not-found marker
            blob=json.dumps(d).lower()
            if "not_found" in blob or "does not exist" in blob or d.get("code")==404:
                return None, "gone"
        time.sleep(5)
    return None, "unknown"

allvgs=set()
for vmu in uuids:
    g, state = vm_get(vmu)
    if state=="gone": print("VM",vmu[:8],"confirmed gone"); continue
    if state=="unknown":
        print("FATAL: cannot determine whether VM %s exists (PC unreachable/erroring). "
              "REFUSING to report teardown success — re-run when PC is healthy."%vmu[:8])
        sys.exit(9)
    name=g["status"]["name"]
    # 1. power OFF
    if g["status"]["resources"].get("power_state")=="ON":
        spec={"metadata":{k:v for k,v in g["metadata"].items() if k!="status"},"spec":g["spec"]}
        spec["spec"]["resources"]["power_state"]="OFF"
        api("PUT",V3+"/vms/"+vmu,spec)
        for _ in range(20):
            time.sleep(8)
            if json.loads(api("GET",V3+"/vms/"+vmu) or "{}").get("status",{}).get("resources",{}).get("power_state")=="OFF": break
        print("VM",name,"powered OFF")
    # 2. detach every attached VG (spec+status views)
    g=json.loads(api("GET",V3+"/vms/"+vmu) or "{}")
    vgs=set()
    for src in (g.get("spec",{}).get("resources",{}).get("disk_list",[]),g.get("status",{}).get("resources",{}).get("disk_list",[])):
        for d in src:
            ref=d.get("volume_group_reference")
            if ref: vgs.add(ref["uuid"])
    for v in vgs: api("POST",VOL+"/%s/$actions/detach-vm"%v,{"extId":vmu})
    allvgs|=vgs
    if vgs: time.sleep(20)
    # 3. delete VM
    api("DELETE",V3+"/vms/"+vmu); print("VM delete",name,vmu[:8])
time.sleep(25)
# 4. delete the detached VGs + any claim-named VGs
for v in allvgs: api("DELETE",VOL+"/"+v)
pg=0
while True:
    d=None
    for _try in range(3):   # PC truncates large list bodies under load — retry the page
        try:
            d=json.loads(api("GET",VOL+"?$page=%d&$limit=100"%pg) or "{}").get("data",[]); break
        except Exception:
            time.sleep(5)
    if d is None: print("WARN: VG page %d unreadable after retries — sweep incomplete"%pg); break
    if not isinstance(d,list) or not d: break
    for v in d:
        if v.get("name","").startswith(claim+"-"): api("DELETE",VOL+"/"+v["extId"]); print("VG delete",v["name"])
    pg+=1
print("teardown done (map-scoped, %d attached VGs handled)"%len(allvgs))
