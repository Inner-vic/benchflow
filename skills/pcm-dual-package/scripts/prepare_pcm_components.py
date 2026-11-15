"""Prepare isolated component recipes for the unchanged PCM Jenkins job."""
import argparse
import ast
import difflib
import hashlib
import json
import pathlib
import re


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_nodes(text, changes):
    offsets = [0]
    for line in text.splitlines(True):
        offsets.append(offsets[-1] + len(line.encode('utf-8')))
    data = text.encode('utf-8')
    spans = [(offsets[node.lineno - 1] + node.col_offset,
              offsets[node.end_lineno - 1] + node.end_col_offset, value.encode('utf-8'))
             for node, value in changes]
    for start, end, value in sorted(spans, reverse=True):
        data = data[:start] + value + data[end:]
    return data.decode('utf-8')


def recipe(text, name, version, commit, mode=None, lidar_ref=None):
    tree = ast.parse(text)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if len(classes) != 1:
        raise ValueError('Expected one reviewed Conan recipe class')
    cls = classes[0]
    names = [n.value.value for n in cls.body if isinstance(n, ast.Assign) and
             any(isinstance(t, ast.Name) and t.id == 'name' for t in n.targets) and
             isinstance(n.value, ast.Constant)]
    if names != [name]:
        raise ValueError('Component recipe name does not match')
    versions = [n.args[0] for n in ast.walk(cls) if isinstance(n, ast.Call) and
                isinstance(n.func, ast.Attribute) and n.func.attr == 'semver' and
                n.args and isinstance(n.args[0], ast.Constant)]
    branches = [n.value for n in cls.body if isinstance(n, ast.Assign) and
                any(isinstance(t, ast.Name) and t.id == 'branch' for t in n.targets)]
    if len(versions) != 1 or len(branches) != 1:
        raise ValueError('Cannot bind component version and source commit safely')
    changes = [(versions[0], repr(version)), (branches[0], repr(commit))]
    if lidar_ref:
        requires = [n.args[0] for n in ast.walk(cls) if isinstance(n, ast.Call) and
                    isinstance(n.func, ast.Attribute) and n.func.attr == 'requires' and
                    n.args and isinstance(n.args[0], (ast.Constant, ast.JoinedStr)) and
                    ast.get_source_segment(text, n.args[0]).lstrip('f').strip('\"\'').startswith('xlidar/')]
        if len(requires) != 1:
            raise ValueError('Cannot bind the common X-Lidar dependency safely')
        changes.append((requires[0], repr(lidar_ref)))
    result = replace_nodes(text, changes)
    # The business recipes clone into a child directory, but configure() and
    # package() read from source_folder itself. Normalize only our private
    # recipe's Git acquisition; the shared pipeline and business tree stay put.
    sources=[n for n in ast.walk(ast.parse(result)) if isinstance(n,ast.FunctionDef) and n.name=='source']
    if sources:
        repositories={'uroad_m100':'https://gitlab.chehejia.com/livc-acore/chassis/uroad.git',
                      'xlidar':'https://gitlab.chehejia.com/livc-acore/framework-platform/xlidar.git'}
        repository=repositories[name]
        if len(sources)!=1 or repository[:-4].split('://',1)[1] not in ast.get_source_segment(result,sources[0]):
            raise ValueError('Review the component source transport before adapting it')
        askpass='#!/usr/bin/env python3\nimport os,sys\nprint("oauth2" if "username" in sys.argv[1].lower() else os.environ.get("GITLAB_ACCESS_TOKEN", ""))\n'
        source_method="""def source(self):
        import os, pathlib, tempfile
        token = tools.get_env("GITLAB_ACCESS_TOKEN")
        if not token:
            raise ValueError("Jenkins GitLab credential is unavailable")
        with tempfile.TemporaryDirectory(prefix="pcm-git-askpass-") as temporary:
            helper = pathlib.Path(temporary) / "askpass.py"
            helper.write_text(ASKPASS_CODE, encoding="utf-8")
            helper.chmod(0o700)
            with tools.environment_append({"GIT_ASKPASS": str(helper), "GIT_TERMINAL_PROMPT": "0"}):
                self.run("git init")
                self.run("git -c credential.helper= fetch --depth=1 --no-tags " + REPOSITORY + " " + self.branch)
                self.run("git checkout FETCH_HEAD")""".replace('REPOSITORY',repr(repository)).replace('ASKPASS_CODE',repr(askpass))
        result=replace_nodes(result,[(sources[0],source_method)])
    # No new Conan option is required at the Jenkins consumer. Each unique
    # recipe fixes the effective CMake value before configure().
    switch, value = ('WITH_EVAL_DUMP', mode) if mode else ('WITH_REPLAY', 'ON')
    def switch_target(target):
        if not isinstance(target, ast.Subscript): return False
        key=target.slice
        if isinstance(key, ast.Index): key=key.value
        return isinstance(key, ast.Constant) and key.value==switch
    existing = [n for n in ast.walk(ast.parse(result)) if isinstance(n, ast.Assign) and any(switch_target(t) for t in n.targets)]
    # X-Lidar's original option mapping is retained; the final fixed assignment
    # overrides it in the private recipe. Reject a pre-modified M100 input.
    if existing and (mode is not None or len(existing)!=1):
        raise ValueError('Start from the original recipe without an existing switch binding')
    lines = result.splitlines(True)
    configure = [i for i, line in enumerate(lines) if re.search(r'self\._cmake\.configure\(', line)]
    if len(configure) != 1:
        raise ValueError('Cannot identify the reviewed CMake configure boundary')
    index = configure[0]
    indent = re.match(r'\s*', lines[index]).group()
    lines.insert(index, indent + 'self._cmake.definitions[' + repr(switch) + '] = ' + repr(value) + '\n')
    result = ''.join(lines)
    ast.parse(result)
    return result


def prepare(uroad, lidar, source_commit, lidar_commit, version_base, pair_id, output, lidar_version_base=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{7,47}', pair_id):
        raise ValueError('Use a unique lowercase pair ID, 8 to 48 characters')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version_base):
        raise ValueError('Component base version must be X.Y.Z')
    if not isinstance(lidar_version_base,str) or not re.fullmatch(r'\d+\.\d+\.\d+',lidar_version_base):
        raise ValueError('Resolve the X-Lidar base version from this input; no historical default')
    if any(not re.fullmatch(r'[0-9a-f]{40}', v) for v in [source_commit, lidar_commit]):
        raise ValueError('Both source commits must be pinned')
    if output.exists():
        raise ValueError('Output already exists; never overwrite component identities')
    source = pathlib.Path(uroad, 'conanfile.py').read_text(encoding='utf-8')
    lidar_source = pathlib.Path(lidar, 'conanfile.py').read_text(encoding='utf-8')
    channel = 'pcm-' + pair_id
    # A private channel prevents these internal recipes from replacing stable
    # releases or entering stable-channel range resolution on other branches.
    lidar_version = lidar_version_base + '-pcm.' + pair_id
    lidar_ref = 'xlidar/' + lidar_version + '@livc/' + channel
    manifest = {'schema': 'pcm-existing-components/v1', 'pair_id': pair_id,
                'source_commit': source_commit, 'xlidar_commit': lidar_commit,
                'channel': channel, 'status': 'LOCAL_RECIPES_ONLY',
                'published': False, 'components': {}}
    for mode in ['OFF', 'ON', 'xlidar']:
        name = 'xlidar' if mode == 'xlidar' else 'uroad_m100'
        version = lidar_version if mode == 'xlidar' else version_base + '-pcm-' + mode.lower() + '.' + pair_id
        before = lidar_source if mode == 'xlidar' else source
        after = recipe(before, name, version, lidar_commit if mode == 'xlidar' else source_commit,
                       None if mode == 'xlidar' else mode,
                       None if mode == 'xlidar' else lidar_ref)
        folder = output / mode
        folder.mkdir(parents=True)
        (folder / 'conanfile.py').write_text(after, encoding='utf-8')
        (folder / 'recipe.patch').write_text(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/conanfile.py', tofile='b/conanfile.py')), encoding='utf-8')
        manifest['components'][mode] = {'ref': name + '/' + version + '@livc/' + channel,
            'recipe_sha256': digest(after.encode()), 'source_recipe_sha256': digest(before.encode()),
            'source_commit': lidar_commit if mode == 'xlidar' else source_commit,
            'cmake': {'WITH_REPLAY' if mode == 'xlidar' else 'WITH_EVAL_DUMP': 'ON' if mode == 'xlidar' else mode}}
    (output / 'components.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uroad-source', type=pathlib.Path, required=True)
    parser.add_argument('--xlidar-source', type=pathlib.Path, required=True)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--xlidar-commit', required=True)
    parser.add_argument('--version-base', required=True)
    parser.add_argument('--xlidar-version-base', required=True)
    parser.add_argument('--pair-id', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.uroad_source, args.xlidar_source, args.source_commit,
          args.xlidar_commit, args.version_base, args.pair_id, args.output, args.xlidar_version_base), ensure_ascii=False))


if __name__ == '__main__':
    main()
