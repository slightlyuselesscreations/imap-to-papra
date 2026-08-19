# imap-to-papra

Takes attachments out of an IMAP mailbox and puts them into [Papra](https://papra.app).

Papra can already ingest documents by email, but both documented options route your mail through a third party: the hosted OwlRelay service, or a Cloudflare Email Worker. This script talks to your mailbox directly instead, in an effort to keep this transit entirely self-hosted.

I have a sieve filter on my mail server that copies any mail with an attachment
worth keeping into a separate mailbox. This script empties that mailbox: it finds
the unread mail, uploads the attachments to Papra, checks they arrived, and then
deletes the mail. It knows nothing about sieve, it just drains whatever mailbox
you point it at.

It runs once and exits. Scheduling is left to cron, a systemd timer, or the cron
daemon inside the Docker image.

My sieve filter looks like this:

```sieve
require ["copy", "mime"];

if anyof (
    header :mime :anychild :param "filename" :matches "Content-Disposition" "?*",
    header :mime :anychild :param "name" :matches "Content-Type" "?*",
    header :mime :anychild :contains "Content-Disposition" "attachment"
)
{
    redirect :copy "papra@wise.wtf";
}
```

In case you are using `mailcow-dockerized` like me, this can go as an admin configured filter for the mailbox you want it to act upon. (IE: Your main mailbox)

## What it does

* Connects to IMAP over TLS and looks for unread messages
* Picks out the real attachments, skipping inline images (signature logos and the
  like) and noise such as smime.p7s
* Uploads each one to Papra and verifies it by reading it back
* Deletes the message, but only if every attachment made it
* Optionally sends an ntfy notification saying what was filed, from whom, and
  with what subject

If anything goes wrong the message is left unread and gets retried next run.
Papra refuses duplicate files, so a retry after a half finished run will not
create a second copy of anything.

## Installation

### Docker

```yaml
services:
  imap-to-papra:
    image: ghcr.io/slightlyuselesscreations/imap-to-papra:latest
    restart: unless-stopped
    environment:
      CRON_SCHEDULE: "*/5 * * * *"
    volumes:
      - ./config.toml:/config/config.toml:ro
```

Make sure the `config.toml` is mounted into the container. Like in the example.

### From source

Needs Python 3.11 or newer.

```bash
pip install .
```

## Configuration

Copy `config.example.toml` to `config.toml` and fill it in. Everything is
commented in there. This is the minimal configuration required for the script to work:

```toml
[imap]
host = "mail.example.com"
username = "papra-intake@example.com"
password = "..."
on_success = "delete"      # delete, move or mark_read

[papra]
base_url = "https://papra.example.com"
api_key = "..."
organization_id = "org_..."
```

The API key needs the `documents:create` and `documents:read` permissions.

## Usage

```bash
imap-to-papra --config config.toml
```

Check the IMAP and Papra settings without touching any mail:

```bash
imap-to-papra --check
```

See what it would upload, without uploading or deleting anything:

```bash
imap-to-papra --dry-run
```

There are systemd unit files and a crontab example in `systemd/` for running it
outside Docker.

## Notes

Start with `on_success = "mark_read"` or `"move"` if you want the first few runs
to be reversible. Switch to `"delete"` once you trust it.

An attachment that is too big for Papra will not be silently dropped. The
message is left in the mailbox and reported as a failure, so nothing gets lost.

## AI usage disclaimer

The code in this repository was written by Claude Opus 5 with High reasoning, the only human work has gone into the concept and into making sure that it worked properly.
