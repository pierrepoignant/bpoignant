"""Daily check that the Google Drive connection still works.

Google refresh tokens are long-lived but not permanent: they are revoked when
the user changes password, when access is withdrawn, and — the common case
here — after seven days while the OAuth consent screen is still in *Testing*
publishing status. Without this check the breakage is discovered by Bernard,
mid-article, when the import silently fails.

A failure records the state (admin banner) and e-mails the administrators once
per outage. A success clears it. Network trouble is not treated as an expired
token, so a blip does not raise a false alarm.

Usage:
    python check_gdrive.py                 # against OVH (prod) by default
    python check_gdrive.py --db local
"""

import argparse
import sys

from __init__ import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='ovh', choices=['ovh', 'local'],
                        help='which database to use (default: ovh)')
    args = parser.parse_args()

    app = create_app(args.db)
    with app.app_context():
        import gdrive
        from init_db import db

        if not gdrive.has_client_credentials():
            print("Google Drive n'est pas configuré — rien à vérifier.")
            return

        ok, message = gdrive.health_check()
        db.session.commit()
        print(('OK — ' if ok else 'ÉCHEC — ') + message)
        if not ok:
            # Non-zero so the CronJob is marked failed and shows up in history.
            sys.exit(1)


if __name__ == '__main__':
    main()
