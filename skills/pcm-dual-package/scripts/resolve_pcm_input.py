"""Normalize GitLab branch, repository-name and repository-link inputs; no I/O."""
import argparse
import json
import re
from urllib.parse import unquote, urlsplit

HOST='gitlab.chehejia.com'
UROAD='https://gitlab.chehejia.com/livc-acore/chassis/uroad.git'
PROJECT='livc-acore/chassis/uroad'


def branch(value):
    if not value or value.startswith(('-', '/', '.')) or value.endswith(('/', '.', '.lock')) or any(x in value for x in ['..','//','@{']):
        raise ValueError('Invalid branch')
    if re.search(r'[\s~^:?*\[\\\x00-\x1f]',value): raise ValueError('Invalid branch')
    return value


def project(value):
    value=value.strip('/')
    if value.endswith('.git'): value=value[:-4]
    if '/' not in value or any(p in ['.','..',''] or not re.fullmatch(r'[A-Za-z0-9_.-]+',p) for p in value.split('/')):
        raise ValueError('A complete GitLab project path is required')
    return 'https://'+HOST+'/'+value+'.git'


def repository(value):
    if value in ['uroad',PROJECT,UROAD,UROAD[:-4]]: return UROAD
    if value.startswith('git@'+HOST+':'): return project(value.split(':',1)[1])
    if value.startswith('https://'):
        parsed=urlsplit(value)
        if parsed.hostname!=HOST or parsed.port not in [None,443] or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use a company GitLab URL without embedded credentials')
        return project(unquote(parsed.path))
    return project(value)


def resolve(value,kind='auto',explicit_branch=None,context_repository=None,context_branch=None):
    value=value.strip()
    if not value: raise ValueError('A GitLab branch, repository name or link is required')
    repo=None; selected=None; from_url=False
    if value.startswith('https://'):
        parsed=urlsplit(value)
        if parsed.hostname!=HOST or parsed.port not in [None,443] or parsed.username or parsed.password or parsed.fragment or parsed.query not in ['', 'ref_type=heads']:
            raise ValueError('Use a company GitLab URL without credentials or unsupported query fields')
        path=unquote(parsed.path)
        if '/-/tree/' in path:
            project_path,selected=path.split('/-/tree/',1)
            repo=project(project_path); selected=branch(selected.rstrip('/')); from_url=True
        elif '/-/' in path:
            raise ValueError('Use the repository home or branch link')
        else: repo=project(path)
    elif value in ['uroad',PROJECT] or value.startswith('git@'+HOST+':') or (kind=='repository' and '/' in value):
        repo=repository(value)
    elif kind=='branch' or (kind=='auto' and context_repository):
        repo=repository(context_repository) if context_repository else None
        selected=branch(value)
        if repo is None:
            return {'status':'NEEDS_REPOSITORY','branch':selected,'input_kind':'branch'}
    elif kind=='repository' or kind=='auto':
        if '/' in value:
            repo=project(value)
        else:
            return {'status':'NEEDS_REPOSITORY_LOOKUP','repository_hint':value,'branch':branch(explicit_branch) if explicit_branch else None,'input_kind':'repository_name'}
    else: raise ValueError('Unsupported input kind')
    if explicit_branch:
        explicit_branch=branch(explicit_branch)
        if selected and selected!=explicit_branch: raise ValueError('Explicit branch conflicts with branch URL')
        selected=explicit_branch
    used_context=False
    if not selected and context_repository and context_branch and repository(context_repository)==repo:
        selected=branch(context_branch); used_context=True
    result={'repository':repo,'branch':selected,'input_kind':'branch_link' if from_url else ('branch' if kind=='branch' else 'repository'),
            'branch_from_existing_context':used_context,'compatible_with_current_pcm_job':repo==UROAD,
            'output':{'OFF':'APP 普包','ON':'APP 普包'},'changes_shared_pipeline':False}
    if not selected: result['status']='NEEDS_BRANCH'
    elif repo!=UROAD: result['status']='REPOSITORY_COMPATIBILITY_CHECK_REQUIRED'
    else: result['status']='READY_TO_PIN_COMMIT'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True,help='Internal JSON: value, kind, branch and existing context')
    parser.add_argument('--output')
    args=parser.parse_args()
    with open(args.request,encoding='utf-8-sig') as stream: request=json.load(stream)
    result=resolve(request['value'],request.get('kind','auto'),request.get('branch'),request.get('context_repository'),request.get('context_branch'))
    text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        with open(args.output,'w',encoding='utf-8') as stream: stream.write(text)
    print(text,end='')


if __name__=='__main__': main()
