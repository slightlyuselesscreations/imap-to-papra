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

## Forwarded mail

Forwarding rewrites `From` to whoever forwarded it, which would file a
forwarded invoice under a colleague instead of the company that sent it. The
original headers are recovered where possible, and `Email sender`,
`Email subject` and `Email date` all describe the original mail:

| Forward style | Where the original is read from | How well it works |
| --- | --- | --- |
| Forward as attachment | The `message/rfc822` part | Exactly. Real headers, no guessing |
| Ordinary forward | The header block the client pastes at the top of the body | Sender and subject reliably; the date often not |

The pasted block is read in English, German, French, Italian, Spanish, Dutch,
the Nordic languages and Polish, from plain text or from HTML when that is all
the mail carries. A mail that is not a forward is untouched.

The date is the weak spot: clients rewrite it into their own local format
("Sent: Tuesday, August 25, 2026 9:00 AM", or Gmail's "Tue, 25 Aug 2026 at
09:00"), and neither is a date any parser is obliged to understand. When the
original date cannot be read, `Email date` falls back to when the mail was
forwarded rather than guessing. An attached original always has a real date.

Note that a forward-as-attachment holds two files as far as Papra is concerned:
the attached `.eml` and the attachment inside it. Both are archived. To keep
only the real document, add `eml` to `attachments.denied`.

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
