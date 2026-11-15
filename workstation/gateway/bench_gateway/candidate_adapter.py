"""Offline binding of one reviewed Stream protocol to this user's frozen inputs.

This module never opens a file, imports template code, or contacts a bench.
Rendering is not compatibility evidence: every package still needs its own gate.
The narrowly pinned protocol uses camera 50053 / lidar 50054 and GT OFF.
"""
import ast
import copy
import hashlib
import json
from pathlib import PurePosixPath
import re

TEMPLATE_ID = "sw0058-v151-m1004-smoke-20261012-001"
TEMPLATE_HASHES = {
    "job.json": "f6f8e67573912e6798a5022c9ca0b68073d10f99b6666c5e27021d60fe4bb711",
    "profile.json": "a6de45cd902a39156d4f19d077558a1bebe36c462a07395bc2672c9f7a5afa31",
    "small_stream_adapter.py": "333b557136a6110d27ff3a769132dc570ef0d5befc7c564fd2532b6552740ef3",
    "sw0058_gate_probe_driver_reference.py": "95d1f55505f42f1423c09d925aaece43d34fa7d209d01a1ad925154fcb3e4d06",
}
CONTROL_HASHES = {
    "bin/bench-replay-mode": "cb0d617cfcf0a48cab87e8ff042cf50d3a5e26fd8dc78b3b1f6144b761aafcac",
    "bin/formal-replay-wrapper.sh": "4d97a9f376a4a50b91c13c5098426976a94e5bc165059eb43a4662f76c4d49ed",
    "sdf/infer_expert_pcm_replay.sdf": "7c0eb2c9bc53a188336522d047ac7dfb4caa1364ffc182a0465242b12d7dc225",
}
COMMON = "etc/uroad/config/common_param.yaml"
SO_TARGETS = ("lib/liburoad_m100.so", "lib/libxcamera.so", "lib/libxlidar.so")
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA = re.compile(r"^[0-9a-f]{64}$")
GT_FIELD = 'eval_dump.gt_trajectory.enable'
NORMALIZATION_RULE = 'sw0058-build2737-m1004-gt-off-v1'
NORMALIZATION_INPUTS = {('0c640e3c54adb59d1612f97b9f684bccf5bf3663a1b9ea1646b3c72f40714788',
                         '54b4c81128b4be576166efd4a6516559e208c8f5b2419a12e6b0a835ccd2ecf2', TEMPLATE_ID)}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("invalid SHA256 identity")
    return value


def absolute(value):
    if (not isinstance(value, str) or not value.startswith("/") or "\\" in value
            or any(ord(c) < 32 for c in value) or str(PurePosixPath(value)) != value
            or ".." in PurePosixPath(value).parts):
        raise ValueError("invalid canonical workstation path")
    return value


def target(value):
    if not isinstance(value, str):
        raise ValueError("invalid replacement target")
    for prefix in ("/app/livc/", "APP/livc/", "livc/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if (not value or value.startswith("/") or "\\" in value
            or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value):
        raise ValueError("invalid APP-relative replacement target")
    return value


def descriptor(value, owner=None):
    if not isinstance(value, dict):
        raise ValueError("missing frozen material descriptor")
    path = absolute(value.get("remote_path") or value.get("path"))
    if owner and (path == owner or owner not in [str(p) for p in PurePosixPath(path).parents]):
        raise ValueError("candidate material is outside this user's directory")
    return {"path": path, "sha256": sha(value.get("sha256"))}


def template_check(template):
    if not isinstance(template, dict) or template.get("id") != TEMPLATE_ID:
        raise ValueError("unsupported reviewed Stream template")
    sources = {"job.json": template.get("job_text"), "profile.json": template.get("profile_text"),
               "small_stream_adapter.py": template.get("adapter"),
               "sw0058_gate_probe_driver_reference.py": template.get("driver")}
    for name, text in sources.items():
        if (not isinstance(text, str) or len(text) > 1024 * 1024
                or digest(text) != TEMPLATE_HASHES[name]
                or template.get("hashes", {}).get(name) != TEMPLATE_HASHES[name]):
            raise ValueError("reviewed template source hash differs: " + name)
    for name in ("job", "profile"):
        if json.loads(sources[name + ".json"]) != template.get(name):
            raise ValueError("template object differs from pinned source: " + name)


def config_fields(text):
    """Use the same narrow scalar grammar as the reviewed driver's overlay.

    Reject unsupported/duplicate required fields before any execution. Full YAML
    syntax and semantic validation belong to the upstream YAML preparation step.
    """
    if not isinstance(text, str) or len(text.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("candidate common YAML is missing or too large")
    if text.startswith("\ufeff"):
        raise ValueError("reviewed replay driver requires common YAML without a UTF-8 BOM")
    fields, section, nested = {}, "", ""
    for line in text.lstrip("\ufeff").splitlines():
        top = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?", line)
        if top:
            section, nested = top.group(1), ""
            continue
        sub = re.fullmatch(r"  ([A-Za-z_][A-Za-z0-9_]*):\s*(?:#.*)?", line)
        if sub:
            nested = sub.group(1)
            continue
        item = re.fullmatch(r"(  |    )([A-Za-z_][A-Za-z0-9_]*):\s*(\S+)(.*)", line)
        if not item or not section:
            continue
        indent, key, scalar, suffix = item.groups()
        name = section + ("." + nested if indent == "    " else "") + "." + key
        if name in fields:
            raise ValueError("duplicate common YAML field: " + name)
        # A scalar containing spaces, flow collections or multiline content is
        # outside the pinned driver's grammar; do not silently truncate it.
        fields[name] = scalar if not suffix.strip() or suffix.lstrip().startswith("#") else None
    return fields


def boolean(value, name):
    if not isinstance(value, str) or value.lower() not in ("on", "off", "true", "false"):
        raise ValueError("missing or unsupported common YAML field: " + name)
    return value.lower() in ("on", "true")


def configuration_overrides(run_id,eval_dump=True):
    """The reviewed protocol's fixed overlay, also shown on the business task card."""
    values = {"replay.enable": True, "bench_mode.enable": True,
              "speed_gate.enable": False, "perception_gate.enable": False,
              "apa_gate.enable": False, "gear_gate.enable": False,
              "eval_dump.enable": eval_dump, "eval_dump.gt_trajectory.enable": False,
              "eval_dump.output_dir": "/app/livc-test/" + run_id + "/outputs/eval_pred_result",
              "eval_dump.scene_root": "/app/livc-test/" + run_id + "/scene-markers"}
    if not eval_dump:values.pop("eval_dump.gt_trajectory.enable")
    return values


def normalization_digest(record):
    return hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode('utf-8')).hexdigest()


def protocol_normalize_common(original,current,*,package_sha256,m100_sha256,template_id,
                              package_source_sha256=None,package_origin='personal',
                              user_operations=(),replacements=()):
    """Only the reviewed old schema may gain the protocol's missing GT OFF node.

    This is a pure, exact text derivation, not a user YAML edit. Assembly lineage
    is accepted only after the remote caller binds its catalog-origin metadata
    back to the frozen owner assets. Arbitrary raw source-sha overrides fail.
    """
    fields=config_fields(current)
    if GT_FIELD in fields:return current,None
    source=package_source_sha256 or package_sha256
    if package_origin!='assembly_catalog' and source!=package_sha256:
        raise ValueError('raw package cannot override normalization source identity')
    if (source,m100_sha256,template_id) not in NORMALIZATION_INPUTS:return current,None
    if current!=original:raise ValueError('protocol normalization requires unchanged original common YAML')
    if any(target(item.get('target',''))==COMMON for item in user_operations):
        raise ValueError('protocol normalization cannot replace explicit user common YAML changes')
    if any(target(item.get('target',''))==COMMON for item in replacements):
        raise ValueError('protocol normalization cannot replace user common YAML material')
    # The unchanged fields must already satisfy the reviewed scalar grammar.
    for key,value in configuration_overrides('<run_id>').items():
        if key==GT_FIELD:continue
        if isinstance(value,bool):boolean(fields.get(key),key)
        elif not fields.get(key):raise ValueError('missing common YAML field: '+key)
    for key in ('save_traj','save_traj_xy','save_vision'):
        boolean(fields.get('eval_dump.'+key),'eval_dump.'+key)
    lines=original.splitlines(keepends=True);headers=[]
    for index,line in enumerate(lines):
        clean=line.split('#',1)[0].rstrip('\r\n').strip()
        if not clean:continue
        if clean in ('---','...') or '\t' in line[:len(line)-len(line.lstrip())]:
            raise ValueError('ambiguous YAML document for protocol normalization')
        if re.search(r'\bgt_trajectory\b',clean):
            raise ValueError('ambiguous existing gt_trajectory node')
        if re.match(r"['\"]?eval_dump['\"]?\s*:",clean):headers.append(index)
    if len(headers)!=1:raise ValueError('protocol normalization requires one plain eval_dump mapping')
    start=headers[0]
    if not re.fullmatch(r'eval_dump: *(?:#.*)?(?:\r?\n)?',lines[start]):
        raise ValueError('protocol normalization requires one plain eval_dump mapping')
    seen=set()
    for line in lines[start+1:]:
        clean=line.split('#',1)[0].rstrip('\r\n')
        if not clean.strip():continue
        if not clean.startswith(' '):break
        scalar=re.fullmatch(r'  ([A-Za-z_][A-Za-z0-9_]*): +([^\s].*)',clean.rstrip())
        if (not scalar or scalar.group(1) in seen or scalar.group(2)[0] in '{}[]*&!|>'):
            raise ValueError('ambiguous eval_dump mapping for protocol normalization')
        seen.add(scalar.group(1))
    newline='\r\n' if lines[start].endswith('\r\n') else '\n'
    if not lines[start].endswith('\n'):lines[start]+=newline
    normalized=''.join(lines[:start+1])+('  gt_trajectory:'+newline+'    enable: OFF'+newline)+''.join(lines[start+1:])
    record={'schema_version':1,'rule_id':NORMALIZATION_RULE,'target':COMMON,'source':'reviewed-protocol-default',
            'package_sha256':source,'input_package_sha256':package_sha256,'m100_sha256':m100_sha256,
            'template_id':template_id,'before_sha256':digest(original),'after_sha256':digest(normalized),
            'fields':{GT_FIELD:False},'reason':'已审核旧版 schema 缺少 GT 开关，按固定 GT_OFF 协议补齐；非用户 YAML 修改'}
    return normalized,record


def validate_protocol_normalization(candidate,template_id=TEMPLATE_ID):
    record=candidate.get('protocol_normalization')
    derived=[item for item in candidate.get('replacements',[]) if item.get('kind')=='protocol_normalization']
    if not record:
        if derived:raise ValueError('derived protocol YAML has no normalization receipt')
        return None
    replacements=[item for item in candidate.get('replacements',[]) if item.get('kind')!='protocol_normalization']
    m100=[item for item in replacements if item.get('kind')=='dependency' and item.get('target')=='lib/liburoad_m100.so']
    if len(m100)!=1:raise ValueError('normalization requires the frozen M100 identity')
    normalized,expected=protocol_normalize_common(candidate.get('original_configuration_text'),
        candidate.get('original_configuration_text'),package_sha256=sha(candidate['package'].get('sha256')),
        package_source_sha256=candidate.get('package_source_sha256'),package_origin=candidate.get('package_origin','personal'),
        m100_sha256=sha(m100[0].get('sha256')),template_id=template_id,
        user_operations=candidate.get('yaml_plan',{}).get('operations',[]),replacements=replacements)
    if (not expected or record!=expected or candidate['configuration'].get('text')!=normalized
            or candidate['configuration'].get('sha256')!=expected['after_sha256']
            or candidate.get('binding_identity',{}).get('protocol_normalization_sha256')!=normalization_digest(expected)):
        raise ValueError('protocol normalization content or receipt differs from exact derivation')
    def location(item):return (item.get('remote_path') or item.get('path'),item.get('sha256'))
    if (len(derived)!=1 or derived[0].get('target')!=COMMON
            or not location(derived[0])[0] or location(derived[0])!=location(candidate['configuration'])):
        raise ValueError('protocol normalization replacement differs from bound configuration')
    return expected


def official_replay_without_dump(goal, fields, compiled_mode=None):
    """Inspect frozen upstream/user runtime settings; never flip them by goal."""
    value=str(fields.get('eval_dump.enable','')).strip().lower()
    if value not in ('on','off','true','false'):
        raise ValueError('本APP缺少明确的eval_dump.enable配置，请提供配套运行配置。')
    runtime_on=value in ('on','true')
    if compiled_mode is False and runtime_on:
        raise ValueError('本APP锁定M100为OFF，运行配置却开启eval_dump；请核对配套软件与配置，不自动改开关。')
    if goal!='replay' and (compiled_mode is False or not runtime_on):
        raise ValueError('回放后评测需要官方ON软件包和配套落盘配置；当前为OFF或未启用，不会自动改成ON。')
    return goal=='replay' and not runtime_on


def configuration_contract(candidate, run_id):
    normalization=validate_protocol_normalization(candidate)
    fields = config_fields(candidate["configuration"].get("text"))
    forced = configuration_overrides(run_id,not candidate.get("replay_only",False))
    for key, value in forced.items():
        if isinstance(value, bool):
            boolean(fields.get(key), key)
        elif not fields.get(key):
            raise ValueError("missing common YAML field: " + key)
    original = candidate.get("original_configuration_text")
    original_fields = config_fields(original) if original is not None else None
    changes = candidate.get("yaml_plan", {}).get("operations", [])
    if not isinstance(changes, list):
        raise ValueError("invalid YAML change plan")
    explicit = set()
    replaced = False
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("invalid YAML operation")
        if target(change.get("target", "")) != COMMON:
            continue
        op = change.get("operation", change.get("op"))
        if op == "replace":
            replaced = True
        elif op == "set":
            path = change.get("path")
            if isinstance(path, str):
                path = path.split(".")
            if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
                raise ValueError("invalid common YAML set path")
            key = ".".join(path)
            explicit.update(k for k in forced if k == key or k.startswith(key + "."))
        else:
            raise ValueError("unsupported common YAML operation")
    common_replacement = any(target(r["target"]) == COMMON and r.get('kind')!='protocol_normalization' for r in candidate.get("replacements", []))
    if (replaced or common_replacement) and original_fields is None:
        raise ValueError("original common YAML required to review replacement conflicts")
    for key, expected in forced.items():
        actual = boolean(fields[key], key) if isinstance(expected, bool) else fields[key].strip("\"'")
        changed = False
        if original_fields is not None:
            prior = original_fields.get(key)
            prior = False if normalization and key==GT_FIELD and prior is None else (
                boolean(prior, key) if isinstance(expected, bool) else (prior or "").strip("\"'"))
            changed = actual != prior
        if (key in explicit or changed) and actual != expected:
            raise ValueError("YAML request conflicts with reviewed replay field: " + key)
    if candidate.get("replay_only"):return []
    required = []
    for key, output in (("save_traj", "pred_traj"), ("save_traj_xy", "traj_xy"), ("save_vision", "pred_img")):
        enabled = boolean(fields.get("eval_dump." + key), "eval_dump." + key)
        if key != "save_vision" and not enabled:
            raise ValueError("reviewed replay requires eval_dump." + key)
        if enabled:
            required.append(output)
    return required


def assignment_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return assignment_name(node.value) + "." + node.attr
    return ""


def replace_assignments(source, replacements, remove_formal_subscripts=False):
    lines = source.splitlines(keepends=True)
    edits, seen = [], set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        name = assignment_name(node.targets[0])
        if name in replacements:
            if name in seen:
                raise ValueError("duplicate template assignment: " + name)
            seen.add(name)
            edits.append((node.lineno - 1, node.end_lineno, name + " = " + replacements[name] + "\n"))
        elif (remove_formal_subscripts and isinstance(node.targets[0], ast.Subscript)
              and assignment_name(node.targets[0].value) == "FORMAL_BASELINE_HASHES"):
            edits.append((node.lineno - 1, node.end_lineno, ""))
    if seen != set(replacements):
        raise ValueError("reviewed template structure differs")
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    return "".join(lines)


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("reviewed adapter guard structure differs")
    return source.replace(old, new, 1)


STARTUP_SNAPSHOT = '''import json,os,stat
p='/app/eval-LC/logs/infer_expert_pcm_replay.stdout.log'
try:s=os.stat(p,follow_symlinks=False)
except FileNotFoundError:print(json.dumps({'inode':None,'offset':0}))
else:
 if not stat.S_ISREG(s.st_mode):raise ValueError('startup log is not regular')
 print(json.dumps({'device':s.st_dev,'inode':s.st_ino,'offset':s.st_size}))
'''

# Runs once on the bench, inside the existing Runner transaction. Only new log
# bytes count: old failures in this persistent stdout log cannot reject a run.
STARTUP_MONITOR = '''import json,os,re,socket,stat,sys,time
baseline=json.loads(sys.argv[1]);offset=baseline['offset'];starts=set();partial=''
deadline=time.monotonic()+180;ready={};state='ENDPOINT_UNAVAILABLE'
while time.monotonic()<deadline:
 try:
  fd=os.open('/app/eval-LC/logs/infer_expert_pcm_replay.stdout.log',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  with os.fdopen(fd,'rb') as f:
   s=os.fstat(f.fileno())
   if not stat.S_ISREG(s.st_mode):state='STARTUP_LOG_CHANGED';break
   if baseline['inode'] is not None and (s.st_dev,s.st_ino)!=(baseline['device'],baseline['inode']):
    state='STARTUP_LOG_CHANGED';break
   if s.st_size<offset:state='STARTUP_LOG_CHANGED';break
   baseline.update(device=s.st_dev,inode=s.st_ino)
   f.seek(offset);raw=f.read(1024*1024);offset+=len(raw)
  text=partial+raw.decode('utf-8',errors='replace');lines=text.split('\\n');partial=lines.pop()[-1024:]
  for line in lines:
   match=re.search(r'formal_replay_wrapper_started pid=(\\d+)',line)
   if match:starts.add(match.group(1))
   if starts and re.search(r'undefined symbol:|symbol lookup error|Segmentation fault|SIGABRT|stack smashing detected|__stack_chk_fail',line):
    state='APPLICATION_FATAL';break
  if state=='APPLICATION_FATAL':break
  if len(starts)>1:state='RESTART_LOOP';break
 except FileNotFoundError:pass
 for port in (50053,50054):
  try:
   with socket.create_connection(('192.0.2.15',port),timeout=0.25):ready[str(port)]=True
  except OSError:ready[str(port)]=False
 if all(ready.values()):state='READY';break
 time.sleep(1)
print(json.dumps({'state':state,'ports':ready,'observed_starts':len(starts)}))
'''


def startup_health(source):
    """Replace the reviewed template's blind sequential port waits."""
    marker='    case.bench(\n        "nohup sh -c '
    snapshot=('    snapshot = case.bench("python3 -c " + case.shlex.quote(' + repr(STARTUP_SNAPSHOT) + '), timeout=30)\n'
              '    case.write_json(run_dir / "candidate-startup-baseline.json", json.loads(snapshot))\n')
    source=replace_once(source,marker,snapshot+marker)
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='health_candidate')
    lines=source.splitlines(keepends=True)
    lines[node.lineno-1:node.end_lineno]=['''def health_candidate(run_dir: Path, scene: str) -> None:
    identity(run_dir, scene)
    require_runner_lock(run_dir.name)
    baseline = read(run_dir / "candidate-startup-baseline.json")
    report = json.loads(case.bench("python3 -c " + case.shlex.quote(MONITOR) + " " +
                                  case.shlex.quote(json.dumps(baseline)), timeout=200))
    case.write_json(run_dir / "candidate-startup.json", {"run_id": run_dir.name, "scene": scene, **report})
    if report.get("state") != "READY":
        raise case.ProbeError("candidate startup " + report["state"] + "; inspect this run's startup evidence")
'''.replace('MONITOR',repr(STARTUP_MONITOR))]
    return ''.join(lines)


def service_cleanup(driver, adapter):
    """Unload restart registrations even when a crashed process is inactive."""
    node = next(n for n in ast.parse(driver).body
                if isinstance(n, ast.FunctionDef) and n.name == 'restore_formal_app')
    first = node.body[1]  # The reviewed function starts with its docstring.
    if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
            and isinstance(first.value.func, ast.Name) and first.value.func.id == 'bench'):
        raise ValueError('reviewed restore entry differs')
    lines = driver.splitlines(keepends=True)
    lines[first.lineno - 1:first.end_lineno] = ['    stop_registered_apps()\n']
    helpers = '''def registered_apps() -> set[str]:
    rows = bench(f"{SMCTL} listservice").splitlines()
    if not rows or rows[0].split() != ["NAME", "STATE"]:
        raise ProbeError("service registry response is unavailable or malformed")
    return {row.split()[0] for row in rows[1:] if row.strip()}


def stop_registered_apps() -> None:
    registered = registered_apps()
    for name in ("infer_expert_pcm_replay", "infer_expert_fusion"):
        if name in registered:
            bench(f"{SMCTL} unloadsdf {name}", timeout=300)


'''
    driver = replace_once(''.join(lines), 'def restore_formal_app(', helpers + 'def restore_formal_app(')
    adapter = replace_once(adapter, 'if current == baseline["services"]:',
        'if (current == baseline["services"]\n'
        '                    and "infer_expert_pcm_replay" not in case.registered_apps()):')
    return driver, adapter


def replay_extensions(source, mode, raw_originals):
    """Derive private clients; leave the reviewed shared Recorder tools intact."""
    if mode not in ('stream','frame_by_frame'):raise ValueError('unsupported replay mode')
    if mode=='stream' and not raw_originals:return source
    helper='''def gateway_replay_tools(run_dir):
    import ast, hashlib, shutil
    directory = run_dir / 'configured-inputs/gateway-replay-tools'
    directory.mkdir(parents=True, exist_ok=True)
    source_root = TERMINAL_ADAPTER
    sweep = (source_root / 'run_controlled_sweep.py').read_text()
    native = (source_root / 'native_stream_replay.py').read_text()
    expected = ('b874c7ed8dc11bf29152440a2c37f50985e2a704ceba2093826818de72bd0f95', 'b24764580a9ee6566365a2580767b947ac0cf9e18eb7fd127b76cd90a07fe6b1')
    if tuple(hashlib.sha256(x.encode()).hexdigest() for x in (sweep,native)) != expected:
        raise case.ProbeError('reviewed replay clients changed; refuse derived tools')
    needle = '{"mode": "stream", "speed": args.speed}'
    if native.count(needle) != 1:raise case.ProbeError('replay start structure changed')
    native = native.replace(needle, '{"mode": '+repr(MODE)+', "speed": args.speed}')
    native = native.replace('Recorder-native-stream', 'Recorder-native-'+MODE)
    lines=sweep.splitlines(keepends=True);edits=[]
    for node in ast.parse(sweep).body:
        if isinstance(node,ast.FunctionDef) and node.name=='prepare_runtime':
            text='def prepare_runtime(adapter_dir, runtime, proto_dir, python):\\n    runtime.mkdir(parents=True, exist_ok=True)\\n    shutil.copy2('+repr(str(directory/'native_stream_replay.py'))+', runtime / "native_stream_replay.py")\\n'
            edits.append((node.lineno-1,node.end_lineno,text))
        if RAW and isinstance(node,ast.FunctionDef) and node.name=='recorded_lidar_frame_count':
            body=node.body[0]
            text='    if item.get("replay_input_format")=="cloud-original-v1":\\n        count=item.get("lidar_frames")\\n        if type(count) is not int or count<=0:raise RuntimeError("raw LiDAR count unavailable")\\n        return count\\n'
            edits.append((body.end_lineno,body.end_lineno,text))
    if len(edits)!=(2 if RAW else 1):raise case.ProbeError('replay client derivation differs')
    for start,end,text in sorted(edits,reverse=True):lines[start:end]=[text]
    sweep=''.join(lines)
    for name,text in [('native_stream_replay.py',native),('run_controlled_sweep.py',sweep)]:
        compile(text,name,'exec')
        target=directory/name
        if target.exists():
            if target.is_symlink() or target.read_text()!=text:raise case.ProbeError('derived replay tool changed')
        else:
            with target.open('x') as output:output.write(text)
            target.chmod(0o440)
    return directory / 'run_controlled_sweep.py'


'''.replace('MODE',repr(mode)).replace('RAW',repr(raw_originals))
    source=replace_once(source,'def replay(run_dir: Path, scene: str) -> None:',helper+'def replay(run_dir: Path, scene: str) -> None:')
    source=replace_once(source,'    prepared = read(Path(job["replay_input"]["prepared_manifest"]))',
        '    gateway_replay_tools(run_dir)\n    prepared = read(Path(job["replay_input"]["prepared_manifest"]))')
    source=replace_once(source,'str(case.SWEEP), "--from-job"','str(gateway_replay_tools(run_dir)), "--from-job"')
    return source


def render(template, candidate, run_id, actor, scenes=None):
    """Return job/profile/adapter/driver plus identities, without side effects."""
    template_check(template)
    mode=candidate.get('replay_mode','stream')
    if mode not in ('stream','frame_by_frame'):raise ValueError('unsupported replay mode')
    if (not isinstance(run_id, str) or not SAFE.fullmatch(run_id) or len(run_id) > 86
            or not isinstance(actor, str) or not SAFE.fullmatch(actor)):
        raise ValueError("invalid candidate run or actor identity")
    owner = absolute(candidate.get("owner_root"))
    package = descriptor(candidate.get("package"), owner)
    config = descriptor(candidate.get("configuration"), owner)
    if digest(candidate["configuration"].get("text", "")) != config["sha256"]:
        raise ValueError("configuration text differs from frozen bytes")
    manifest = candidate.get("formal_manifest")
    if not isinstance(manifest, dict):
        raise ValueError("missing current package inventory")

    def file_hash(name):
        item = manifest.get(name)
        if not isinstance(item, dict) or item.get("kind") != "file":
            raise ValueError("required candidate file is absent: " + name)
        return sha(item.get("sha256"))

    replacements = candidate.get("replacements")
    if not isinstance(replacements, list):
        raise ValueError("missing frozen replacement inventory")
    own_materials, targets = [], set()
    for raw in replacements:
        name = target(raw.get("target"))
        if name in targets:
            raise ValueError("duplicate candidate replacement target")
        targets.add(name)
        item = descriptor(raw, owner)
        if item["sha256"] != file_hash(name):
            raise ValueError("replacement differs from current package inventory: " + name)
        own_materials.append({"target": name, **item})
    if not set(SO_TARGETS).issubset(targets):
        raise ValueError("candidate requires this user's camera, lidar and M100 replacements")
    if file_hash(COMMON) != config["sha256"]:
        raise ValueError("common YAML differs from installed candidate inventory")
    controls = candidate.get("controls")
    if not isinstance(controls, list):
        raise ValueError("缺少已审核的共享控制工具材料 (controls)")
    control_targets = set()
    for item in controls:
        name = target(item.get("target"))
        record = descriptor(item)
        if (name in control_targets or CONTROL_HASHES.get(name) != record["sha256"]
                or file_hash(name) != record["sha256"]):
            raise ValueError("shared control tool differs from reviewed template: " + name)
        control_targets.add(name)
    if control_targets != set(CONTROL_HASHES):
        raise ValueError("缺少已审核的共享控制工具材料")
    required = configuration_contract(candidate, run_id)
    formal_hashes = {"/app/livc/" + name: file_hash(name) for name in sorted(set(SO_TARGETS) | {COMMON} | targets)}
    sos = {PurePosixPath(name).name: file_hash(name) for name in SO_TARGETS}
    dataset = candidate.get("dataset", {})
    root, prepared = absolute(dataset.get("remote_path")), dataset.get("prepared_scenes")
    manifest_path, manifest_sha = absolute(dataset.get("manifest")), sha(dataset.get("sha256"))
    cameras = dataset.get("camera_roots")
    if (not isinstance(prepared, list) or not prepared or not isinstance(cameras, list)
            or len(cameras) != len(prepared)):
        raise ValueError("dataset requires complete prepared scenes and explicit camera roots")
    indices = list(range(1, len(prepared) + 1)) if scenes is None else scenes
    if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
            or indices != sorted(set(indices)) or any(not 1 <= i <= len(prepared) for i in indices)):
        raise ValueError("invalid selected dataset scene indices")
    selected, camera_map = [], {}
    for index in indices:
        item = copy.deepcopy(prepared[index - 1])
        name = item.get("scene") if isinstance(item, dict) else None
        if not isinstance(name, str) or not SAFE.fullmatch(name) or name in camera_map:
            raise ValueError("invalid or duplicate prepared scene")
        camera = absolute(cameras[index - 1])
        if type(item.get("camera_frames")) is not int or item["camera_frames"] <= 0:
            raise ValueError("invalid prepared camera frame count")
        item["timestamped_jpeg_frames"] = camera
        selected.append(item)
        camera_map[name] = camera
    job, profile = copy.deepcopy(template["job"]), copy.deepcopy(template["profile"])
    prediction = "/app/livc-test/" + run_id + "/outputs/eval_pred_result"
    job["run_id"] = run_id
    job["execution"]["command_set_id"] = run_id
    job["app_artifact"], job["configuration_input"] = package, config
    job["artifacts"]["evaluation_library"]["sha256"] = sos["liburoad_m100.so"]
    job["replay_input"] = {"dataset_root": root, "prepared_manifest": manifest_path,
                           "representative_scene": selected[0]["scene"], "scene_count": len(selected),
                           "scene": run_id, "archive_stats": selected[0]}
    job["formal_app"].update(prediction_root=prediction, scene_root="/app/livc-test/" + run_id + "/scene-markers")
    job["predictions"].update(transfer_set_id="gateway-bench-to-workstation", source_path=prediction,
                              destination_path="/mnt/data/bench-eval/results/" + run_id)
    job["coordination"] = {"allowed_actors": [actor]}
    job["release_package"] = {"isolated_run": "/app/livc-test/" + run_id}
    job["diagnostic"] = {"reason": "Frozen user candidate; compatibility requires this run's small-scene gate"}
    job["batch_stream"].update(batch_id=run_id, scenes=[{"scene": name, "timestamped_jpeg_frames": path}
                                                         for name, path in camera_map.items()])
    job['batch_stream']['mode']=mode
    # Historical GT/postprocess records describe old material; GT is disabled in
    # this protocol and the new configuration/inventory is the sole authority.
    job.pop("gt_trajectory_input", None)
    job.pop("postprocess_input", None)
    profile["profile_id"] = run_id
    command_set = profile["execution"]["command_sets"].pop(TEMPLATE_ID)
    for command in command_set["commands"].values():
        command["argv"][1] = "/home/lixiang/bench-eval/adapters/" + run_id + "/small_stream_adapter.py"
    profile["execution"]["command_sets"] = {run_id: command_set}
    transfer = next(iter(profile["prediction_transfer"]["sets"].values()))
    profile["prediction_transfer"]["sets"] = {"gateway-bench-to-workstation": transfer}
    driver = replace_assignments(template["driver"], {"CANDIDATE_SOS": repr(sos),
                                "FORMAL_BASELINE_HASHES": repr(formal_hashes)}, True)
    adapter = replace_assignments(template["adapter"], {
        "case.FORMAL_BASELINE_HASHES": repr(formal_hashes), "case.CANDIDATE_SOS": repr(sos),
        "EVIDENCE_ROOT": 'case.RUN_ROOT / ' + repr("adapters/" + run_id + "/evidence"),
        "COMMAND_SET": repr(run_id), "SCENES": "frozenset(" + repr(list(camera_map)) + ")"})
    adapter = replace_once(adapter, "len(entries) != 1", "len(entries) != " + str(len(selected)))
    adapter = replace_once(adapter,
        'not item["timestamped_jpeg_frames"].startswith("/mnt/data/bench-eval/adapters/")',
        'item["timestamped_jpeg_frames"] != ' + repr(camera_map) + '.get(item.get("scene"))')
    adapter = startup_health(adapter)
    driver, adapter = service_cleanup(driver, adapter)
    if mode!='stream':adapter=replace_once(adapter,'batch.get("mode") != "stream"','batch.get("mode") != '+repr(mode))
    adapter=replay_extensions(adapter,mode,any(item.get('replay_input_format')=='cloud-original-v1' for item in selected))
    if candidate.get("replay_only"):
        driver,adapter=replay_only_sources(driver,adapter)
        job["workflow"]["targets"]=["replay_complete"]
    # Parsing/compilation cannot execute the imported historical driver.
    compile(adapter, "candidate_adapter.py", "exec")
    compile(driver, "candidate_driver.py", "exec")
    identity = {"template_id": TEMPLATE_ID, "template_hashes": dict(TEMPLATE_HASHES),
                "package": package, "configuration": config, "replacements": own_materials,
                "dataset_manifest": {"path": manifest_path, "sha256": manifest_sha},
                "selected_indices": list(indices), "required_output_types": required,
                "adapter_sha256": digest(adapter), "driver_sha256": digest(driver),'replay_mode':mode,
                "gate_verified": False, "hardware_actions": "NOT_RUN"}
    return {"job": job, "profile": profile, "adapter": adapter, "driver": driver, "identity": identity}

def replay_only_sources(driver, adapter):
    """Derive private scripts. Shared replay clients and evaluator stay intact."""
    # The normal config contract still governs all replay/gate fields. This mode
    # explicitly disables prediction dumping and does not manufacture predictions.
    driver=replace_once(driver,'("eval_dump", "enable"): "ON"','("eval_dump", "enable"): "OFF"')
    # Optional legacy GT field is not part of the dump-disabled runtime contract.
    driver=replace_once(driver,'        ("eval_dump.gt_trajectory", "enable"): "OFF",\n','')
    lines=adapter.splitlines(keepends=True)
    node=next(n for n in ast.parse(adapter).body if isinstance(n,ast.FunctionDef) and n.name=='required_output_types')
    text='''def required_output_types(run_dir):
    import re
    raw=(run_dir/'configured-inputs/common_param.probe.yaml').read_text()
    section='';found=[]
    for line in raw.splitlines():
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*:',line):section=line.split(':',1)[0]
        if section=='eval_dump' and re.match(r'^  enable:',line):found.append(line.split(':',1)[1].split('#',1)[0].strip().lower())
    if found not in (['off'],['false']):raise case.ProbeError('replay-only requires eval_dump.enable OFF')
    return ()

'''
    lines[node.lineno-1:node.end_lineno]=[text]
    adapter=''.join(lines)
    helper='''def replay_only_sweep(run_dir):
    import hashlib
    generated=globals().get('gateway_replay_tools')
    original=(generated(run_dir) if generated else case.SWEEP).read_text()
    expected='b874c7ed8dc11bf29152440a2c37f50985e2a704ceba2093826818de72bd0f95'
    if not generated and hashlib.sha256(original.encode()).hexdigest()!=expected:raise case.ProbeError('reviewed sweep changed')
    old='if not required_types or not set(required_types) <= ALLOWED_TYPES:'
    new='if (not required_types and not args.no_scene_output_gate) or not set(required_types) <= ALLOWED_TYPES:'
    if original.count(old)!=1:raise case.ProbeError('sweep contract changed')
    derived=original.replace(old,new,1)
    directory=run_dir/'configured-inputs/replay-only-tools';directory.mkdir()
    target=directory/'run_controlled_sweep.py';target.write_text(derived)
    return target


'''
    adapter=replace_once(adapter,'def replay(run_dir: Path, scene: str) -> None:',helper+'def replay(run_dir: Path, scene: str) -> None:')
    needle='str(gateway_replay_tools(run_dir)), "--from-job"' if 'str(gateway_replay_tools(run_dir)), "--from-job"' in adapter else 'str(case.SWEEP), "--from-job"'
    adapter=replace_once(adapter,needle,'str(replay_only_sweep(run_dir)), "--from-job"')
    return driver,adapter


def replay_only_delivery(m,p,exit_code):
    """Validate replay/restoration receipts and archive logs, never predictions."""
    import shutil
    from pathlib import Path
    root=m.ROOT/'runs'/p['run_id'];state=m.read(root/'state.json')
    restore=m.read(root/'restore-verification.json')
    evidence=m.BASE/'adapters'/p['run_id']/'evidence'/p['run_id']
    report=m.read(evidence/'batch-result.json')
    expected=[x['scene'] for x in p['scenes']];completed=report.get('completed') or []
    if (exit_code!=0 or state.get('state')!='COMPLETED_TO_REQUESTED_GOAL' or restore.get('restore_ok') is not True
            or report.get('expected')!=len(expected) or [x.get('scene') for x in completed]!=expected
            or any(x.get('state')!='REPLAY_COMPLETE' for x in completed)):
        raise ValueError('replay-only completion or restoration receipt incomplete')
    for row in completed:
        if (row.get('transport')!='Recorder-native-camera-lidar-stream' or type(row.get('recorded_lidar_frames')) is not int
                or row['recorded_lidar_frames']<=0 or row.get('attempt_index')!=1):
            raise ValueError('replay-only transport evidence incomplete')
        attempt=evidence/row['scene']/'attempt-1'
        if m.read(attempt/'transport-result.json')!=row:raise ValueError('scene transport receipt differs from batch')
        native=attempt/'native-stream-replay.txt'
        if not native.is_file() or native.stat().st_size==0 or not isinstance(m.read(attempt/'recorder-final-status.json'),dict):
            raise ValueError('native Recorder completion evidence missing')
    owner=Path(p['replay_owner_root']);destination=owner/'tasks'/p['task_id']
    if not owner.is_absolute() or '..' in owner.parts or not SAFE.fullmatch(p['task_id']):raise ValueError('invalid replay owner/task identity')
    if any(x.is_symlink() for x in (destination,*destination.parents)):raise ValueError('linked replay delivery path')
    def file_sha(path):
        h=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1048576),b''):h.update(chunk)
        return h.hexdigest()
    source_roots=[root,evidence];inventory=[]
    for base in source_roots:
        if base.is_symlink() or base.resolve()!=base:raise ValueError('linked replay evidence root')
        for file in sorted(base.rglob('*')):
            if file.is_symlink():raise ValueError('linked replay evidence file')
            if not file.is_file():continue
            relative=('runner/' if base==root else 'replay/')+str(file.relative_to(base))
            inventory.append({'path':relative,'sha256':file_sha(file),'size':file.stat().st_size})
    record={'schema_version':1,'state':'PASS','kind':'replay_evidence','run_id':p['run_id'],'task_id':p['task_id'],
            'scenes':expected,'replay_complete':True,'restore_verified':True,'eval_dump':False,
            'predictions_generated':False,'evaluation_executed':False,'files':inventory,
            'scope':'Recorder replay completion and service restoration; prediction quality not evaluated'}
    receipt=destination/'replay-completion.json'
    if destination.exists():
        if not p.get('resume_delivery') or not receipt.is_file() or m.read(receipt)!=record:
            raise ValueError('replay-only delivery exists; inspect rather than overwrite')
    else:
        destination.parent.mkdir(parents=True,exist_ok=True);destination.mkdir(mode=0o700)
        for base,label in [(root,'runner'),(evidence,'replay')]:shutil.copytree(base,destination/label)
        m.write(receipt,record)
    for item in inventory:
        file=destination/item['path']
        if not file.is_file() or file.is_symlink() or file.stat().st_size!=item['size'] or file_sha(file)!=item['sha256']:
            raise ValueError('retained replay evidence differs from source receipt')
    return {'state':'PASS','run_id':p['run_id'],'task':str(destination),'logs':str(destination/'runner'),
            'product_kind':'replay_evidence','receipt':str(receipt),'eval_dump':False,'evaluation_executed':False}


def configure_replay_only_batch(m):
    from pathlib import Path
    original_validate=m.offline_validate;original_finish=m.finish_delivery;original_retain=m.validate_and_retain
    def validate(p):
        if not p.get('replay_only'):return original_validate(p)
        import tempfile,sys
        with tempfile.TemporaryDirectory(prefix='replay-only-plan-') as name:
            job=Path(name)/'job.json';profile=Path(name)/'profile.json';m.write(job,p['job']);m.write(profile,p['profile'])
            result=m.command('preflight','--job',str(job))
            if result.get('status')!='READY':raise ValueError('Runner replay-only preflight blocked')
            sys.path.insert(0,str(m.BASE/'current/automation'))
            from target_workflow import validate_target_job
            resolved=validate_target_job(p['job'],profile,m.ROOT/'runs'/p['run_id'])
            if resolved.get('requires_bench') is not True or resolved.get('targets')!={'replay_complete'}:
                raise ValueError('Runner replay-only resource contract differs')
        return {'runner_preflight':'READY','profile':'VALID','eval_dump':False}
    m.offline_validate=validate
    m.finish_delivery=lambda p,code:replay_only_delivery(m,p,code) if p.get('replay_only') else original_finish(p,code)
    def retain(p,code):
        if not p.get('replay_only'):return original_retain(p,code)
        result=replay_only_delivery(m,p,code);return Path(result['task']),Path(result['receipt'])
    m.validate_and_retain=retain
