# Local development

For deploy, env vars, and OAuth setup see **[DEPLOY.md](DEPLOY.md)**.

**Packaging on AWS:** **PAXminer** and **Weaselbot** deploy as **container images** (Docker builds pushed to ECR). **slackblast** and **qsignups** use SAM **zip** packaging (CI uses `--use-container` on `sam build` for Linux-compatible native wheels).

Use a **virtual environment per app** (dependencies differ):

```bash
cd PAXminer && python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-lambda.txt
# repeat for weaselbot (requirements-lambda.txt), slackblast (slackblast/slackblast/requirements.txt), qsignups (qsignups/qsignups/requirements.txt) as needed
```

**slackblast** runtime deps are maintained with **Poetry** under [`slackblast/slackblast/`](../slackblast/slackblast/) (`pyproject.toml`, `poetry.lock`). The committed [`requirements.txt`](../slackblast/slackblast/requirements.txt) is generated for SAM (`poetry export`). Use **Python 3.12** with Poetry locally (`poetry env use python3.12`) so builds match Lambda. After editing `pyproject.toml`, run `poetry update` then `poetry export -f requirements.txt --without-hashes -o requirements.txt`.

CI’s **`requirements-sync`** job re-exports slackblast (and weaselbot) lockfiles when they drift and pushes with the **automation GitHub App** token so Dependabot PRs get a fresh CI run on the new HEAD (a `GITHUB_TOKEN` push would not re-trigger checks and would stall auto-merge).

## Dependabot automation (overview)

Minor/patch Dependabot PRs auto-merge to **`main`**, then **`main` → `prod` → `test`**. Majors retarget to **`test`** first. The prod→test sync uses branch `chore/sync-prod-to-test` (not `head=prod`) so dependency-pin conflicts can be auto-resolved preferring prod.

See [`.github/workflows/dependabot-automerge.yml`](../.github/workflows/dependabot-automerge.yml), [`promote-main-to-prod.yml`](../.github/workflows/promote-main-to-prod.yml), and [`sync-prod-to-test.yml`](../.github/workflows/sync-prod-to-test.yml).

**Tests (mirrors `ci.yml`):** use a separate venv per app to avoid conflicting pins (e.g. `pymysql`). From the repo root, with `DB_ENCRYPTION_KEY` set to any string ≥16 chars for the qsignups handler import test:

```bash
python3.12 -m venv .venv-wb && . .venv-wb/bin/activate && pip install pytest -r weaselbot/requirements-lambda.txt
(cd weaselbot && pytest -q tests/)

python3.12 -m venv .venv-sb && . .venv-sb/bin/activate && pip install pytest -r slackblast/slackblast/requirements.txt
pytest -q slackblast/test/

python3.12 -m venv .venv-qs && . .venv-qs/bin/activate && pip install pytest boto3 -r qsignups/qsignups/requirements.txt
DB_ENCRYPTION_KEY='your-test-key-at-least-16' pytest -q qsignups/testing/
```

`PAXminer/tests/test_BD_Comparer.py` requires `config/credentials_test.ini` and a live database; it is **skipped** when `CI=true` (e.g. in GitHub Actions). Run it only locally with a configured test DB.
