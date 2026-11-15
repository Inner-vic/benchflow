"""Local execution transport for the workstation's deterministic gateway service."""
import os,subprocess,sys

def main():
    args=sys.argv[1:]
    if not args:raise SystemExit(2)
    # Same administrator-built command previously sent to the verified SSH
    # workstation alias. User inputs remain quoted by the original callers.
    command=args[0] if len(args)==1 else ' '.join(args)
    result=subprocess.run(['/bin/sh','-c',command],env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
    raise SystemExit(result.returncode)

if __name__=='__main__':main()
