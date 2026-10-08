"""Single GUI owner per database; OS releases the lock on process termination."""
from pathlib import Path
import os
from .domain import DomainError


def new_demo_database(project):
    from datetime import datetime,timezone
    from .store import bootstrap,uid
    directory=Path(project).resolve()/'work'/'demos'
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uid('demo')+'.sqlite')
    # Exclusive creation, never unlink/replace an existing file or working database.
    with path.open('xb'):pass
    bootstrap(path)
    return path


class DatabaseLease:
    def __init__(self,path):
        self.path=Path(str(Path(path).resolve())+'.gui.lock')
        self.file=None

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        try:
            if self.path.stat().st_size==0:self.file.write(b'0');self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();self.file=None
            raise DomainError('ALREADY_RUNNING','This database already has a GUI server. Open its existing address, or use a separate demo database.') from None
        return self

    def __exit__(self,*args):
        if self.file:self.file.close();self.file=None
