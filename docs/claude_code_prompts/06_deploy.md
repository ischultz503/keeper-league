# Claude Code Prompt — Phase 6: EC2 Deploy

Paste below the line after Phase 5 is committed. Prereqs I handle myself first: AWS account ready; a domain chosen/purchased; the repo pushed to a PRIVATE GitHub repo (never push .env or db.sqlite3 — both are gitignored; verify before pushing).

Context: I've deployed to EC2 before (comfortable with SSH); Docker runs on the server, not my machine. You have my local terminal — once my SSH key and the instance exist, you can run ssh/scp commands directly, with me watching. Walk me through the AWS-console parts I must click myself; script everything that can be scripted.

---

Read CLAUDE.md and docs/deploy_notes.md. Deploy the app to a single EC2 instance with HTTPS.

## 1. Provision (guide me through the console)

- t3.micro (or t4g.micro if we switch the image to arm — your call, tell me the tradeoff), Ubuntu 24.04 LTS, 20GB gp3.
- Security group: 22 from MY current IP only; 80 and 443 from anywhere. Explain each rule.
- Key pair: guide me to create/download one; tell me where to put it on Windows and how to set permissions for ssh.
- Elastic IP allocated and associated — explain why (instance restarts change public IPs; DNS must point at something stable).

## 2. Server setup (you run these over SSH once I confirm the key works)

- apt update/upgrade, install Docker Engine + compose plugin from Docker's official repo (not the old apt docker.io), add the ubuntu user to the docker group.
- Clone the private GitHub repo (walk me through a deploy key or fine-grained PAT — explain the options and pick the simplest safe one).
- Create the production .env ON THE SERVER (never in git): I'll paste values when prompted — SECRET_KEY (fresh, generate it), DEBUG=false, ALLOWED_HOSTS with the domain, DOMAIN for Caddy, FANTASYPROS_API_KEY.
- Upload my local db.sqlite3 (scp) into the ./db/ mount dir — the production DB starts as a copy of my dev DB, which already has all real data (rosters, picks, trades, eligibility, users). Explain the implication: after this point, prod is the source of truth; don't sync back and forth.

## 3. DNS + first launch

- Point an A record for the domain at the Elastic IP (Route 53 or registrar DNS — whichever I have; walk me through it).
- `docker compose up -d --build`; watch Caddy's logs to see the Let's Encrypt issuance happen (teach me what to look for; common failure = DNS not propagated yet — how to check with nslookup/dig).
- Verify: https://<domain> loads with a valid cert, admin login works, board renders, static files styled. http:// redirects to https (Caddy default).
- Reset per-team user passwords via the admin or a management command so I can distribute credentials to the league.

## 4. Operations (small, but do them)

- A `deploy.sh` on the server: git pull, docker compose up -d --build, prune old images. One command for future updates.
- Nightly SQLite backup to S3: create a small private bucket, an IAM role for the instance with put-only access to that bucket (explain why instance role > access keys on disk), and a cron job that copies a timestamped db.sqlite3 snapshot (use sqlite's .backup or stop-copy-start; explain why copying a live SQLite file mid-write is risky). Include a restore note in deploy_notes.md.
- Update docs/deploy_notes.md with: the deploy runbook, how to SSH in, how to read logs (compose logs, Caddy), how to restart, backup/restore.

Commit all authored files (deploy.sh, docs updates — obviously not .env). Out of scope: Cognito (Phase 7), CI/CD, monitoring beyond logs.
