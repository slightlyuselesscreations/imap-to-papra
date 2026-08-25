# imap-to-papra

Takes attachments out of an IMAP mailbox and puts them into [Papra](https://papra.app).

Papra can already ingest documents by email, but both documented options route your mail through a third party: the hosted OwlRelay service, or a Cloudflare Email Worker. This script talks to your mailbox directly instead, in an effort to keep this transit entirely self-hosted.

## What it does

* Connects to IMAP over TLS and looks for unread messages
* Picks out the real attachments, skipping inline images (signature logos and the
  like) and noise such as smime.p7s
* Uploads each one to Papra and verifies it by reading it back
* Labels the document with custom properties with information from where the mail it came from
* Files forwarded mail under whoever originally sent it, not the forwarder
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
    env_file:
      - .env
    environment:
      CRON_SCHEDULE: "*/5 * * * *"
```

The settings come from `.env`, so nothing secret ends up in the compose file
and you can commit it with the rest of your stack. Keep `.env` out of version
control.

### From source

Needs Python 3.11 or newer.

```bash
pip install .
```

## Configuration

Copy `.env.example` to `.env` and fill it in. Everything is commented in there.
This is the minimum needed for the script to work:

```bash
IMAP_HOST=mail.example.com
IMAP_USERNAME=papra-intake@example.com
IMAP_PASSWORD=...

# delete, move or mark_read
IMAP_ON_SUCCESS=delete

PAPRA_BASE_URL=https://papra.example.com
PAPRA_API_KEY=...
PAPRA_ORGANIZATION_ID=org_...
```

Outside Docker the tool reads `./.env` or `/etc/imap-to-papra/.env` by itself.
Environment variables that are already set take precedence over the file.

Comments need their own line. Everything after the `=` is part of the value,
which is also how Docker reads the file.

### API key permissions

Create the key in Papra under **Settings -> API keys** and tick:

| Permission | Papra label | Needed for |
| --- | --- | --- |
| `documents:create` | Create documents | Uploading the attachment |
| `documents:read` | Read documents | Reading it back to verify it stored |
| `documents:update` | Update documents | Setting the custom properties on it |
| `custom-properties:read` | Read custom properties | Finding the properties that already exist |
| `custom-properties:create` | Create custom properties | Creating the ones that do not |

The first two are mandatory and the run aborts without them. The last three are
only needed for labelling: without them documents are still archived, and the
run logs a warning once at startup saying what is missing.

No delete permission is required. This tool never removes anything from Papra.

## Custom properties

On each run the tool looks for these custom properties in the organization and
creates any that are missing, then fills them in on every document it uploads:

| Property | Type | Value |
| --- | --- | --- |
| `Email subject` | text | The mail's subject |
| `Email sender` | text | The sender's address, without the display name |
| `Email import` | boolean | Always true, so mail-sourced documents can be told apart from hand-uploaded ones |
| `Email date` | date | When the mail was sent, from its Date header |
| `Attachment filename` | text | The attachment's name on arrival, which survives a later rename in Papra |

Matching is by name, case-insensitively, so renaming a property in Papra makes
the next run create a fresh one. Papra allows duplicate property names, so
rename with that in mind.

These are searchable: `"email sender":billing@acme.com`, `has:"email import"`.
A property left empty on a document is simply one the mail did not carry -- a
mail with no Date header gets no `Email date`.

## Usage

```bash
imap-to-papra
```

Point it at a specific file with `--env-file /etc/imap-to-papra/.env`.

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

Start with `IMAP_ON_SUCCESS=mark_read` or `move` if you want the first few runs
to be reversible. Switch to `delete` once you trust it.

An attachment that is too big for Papra will not be silently dropped. The
message is left in the mailbox and reported as a failure, so nothing gets lost.

## AI usage disclaimer

The code in this repository was written by Claude Opus 5 with High reasoning, the only human work has gone into the concept and into making sure that it worked properly.
