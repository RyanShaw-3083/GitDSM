backend

gitdsm.py          service and CLI
webs/index.html    page served by the service

Local run
    python3 gitdsm.py serve
    http://127.0.0.1:8011/

On DSM the same files land in /var/packages/gitdsm/target.
The app window starts them. Do not start them by hand.
Assumes `git` is available in PATH (install via DSM Package Center if needed).
