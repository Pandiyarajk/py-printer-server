# Disclaimer

py-printer-server is provided **AS IS**, without warranty of any kind, express
or implied. **Use it entirely at your own risk.**

This tool binds a network port on the machine it runs on and lets anyone who
can log in upload files and send them to a physical printer attached to that
machine. Used carelessly it can spend a stranger's paper and ink, expose
uploaded files to your network, or leave a printer's settings changed if a job
is interrupted.

The author accepts no liability for data loss, data corruption, wasted
consumables, hardware damage, unauthorised access or disclosure of files,
business interruption, or any other direct or consequential damages arising
from the use of this software.

You are responsible for:

- setting `ADMIN_PASSWORD` to a strong value; the server refuses to start
  without it;
- restricting the server to a network you trust (it binds all interfaces);
- verifying the printer and paper tray before printing anything you did not
  author yourself;
- keeping tested backups of anything uploaded through the spool;
- being authorised to print the files you are printing.

This software is not certified for regulated, forensic, safety-critical or
high-assurance use.

`LICENSE` is the governing legal text and prevails wherever it and this
plain-language summary differ.
