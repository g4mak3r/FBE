"""Copy private BarTender assets explicitly; never include them in public source."""
from pathlib import Path
import argparse
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_templates import TEMPLATE_FILENAMES
from workspaces import seller_workspace


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from', dest='source', type=Path, required=True)
    parser.add_argument('--sid', required=True)
    args = parser.parse_args()
    target = seller_workspace(Path(__file__).resolve().parents[1], args.sid) / 'templates'
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for filename in TEMPLATE_FILENAMES:
        src = args.source / filename
        dst = target / filename
        if not src.is_file():
            print('[FBE] Missing template:', filename)
            continue
        try:
            with src.open('rb') as source, dst.open('xb') as output:
                shutil.copyfileobj(source, output)
        except FileExistsError:
            print('[FBE] Existing template kept; not overwritten:', filename)
            continue
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        copied += 1
        print('[FBE] Copied local template:', filename)
    print(f'[FBE] Template import complete: {copied} copied. Existing files were never overwritten.')
