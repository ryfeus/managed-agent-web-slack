# Lambda deployment through S3

## Goal and behavior

`./scripts/deploy.sh` uploads each built Lambda ZIP once to a private S3 bucket
in the application's region. Functions sharing a ZIP use the same object version;
Lambda code updates pass bucket/key/version instead of uploading the ZIP again.
Phase 6 builds six distinct ZIPs: shared buffered Python, private A2A controller,
A2A event sink, TypeScript Web Function, CMA preview and Slack preview Event Functions. The Web Function
uses the same versioned objects through its Terraform-owned SAM stack. The normal
deployment flow remains; `--application-only` skips CMA changes, secret sync and
database migration/seed for an explicitly authorized application cutover.

## Invariants and security

- Terraform owns one persistent bucket and one object per distinct artifact filename.
  The bucket is created on initial setup and reused across releases; its generated
  name stays in Terraform state. A normal redeploy creates no additional bucket.
- SHA-256 detects changed ZIP bytes; unchanged artifacts need no upload.
- Bucket versioning and exact Lambda object-version pins prevent a later upload
  at the same key from changing a reviewed deployment's code selection.
- Uploads depend on versioning, encryption, public-access blocking and TLS-only
  bucket policy. The bucket is in the same region as the functions.
- Deployment credentials handle artifacts; Lambda execution roles gain no bucket
  permissions. The Lambda service gets scoped read/version-read permission for
  the Web Function artifact and its revision ARN. No secrets or generated identifiers enter source control.
- The existing destructive-change guard protects the bucket and objects.

## Failure, retry and concurrency

Terraform's state lock serializes applies. A failed object upload prevents its
dependent Lambda updates. A partial Lambda rollout can be retried through the same
normal command; completed uploads are reused when their bytes are unchanged.
There is no all-functions atomic rollout. Prior object versions are retained;
rollback uses the desired source/build and the normal deployment path. Bucket
deletion cannot force-remove artifacts. Artifact retention cleanup is manual.
Web Functions also retain replaced immutable revisions for a reviewed revision-weight
rollback. See the [Phase 5 staged procedure](../phase-5-rollout.md) before retiring
the old Web routes; restoring their origin after retirement requires restoring resources.

## Verification

Credential-free Terraform mock tests exercise the actual resource graph: one
upload for the shared buffered package, correct version/hash selection for every
function, and private encrypted versioned storage. Full verification includes
these tests after building the ZIPs. Existing deployment-guard tests protect the
new storage from accidental destruction.

Live acceptance checks uploaded versions, deployed code hashes and successful Lambda updates against the saved plan. The 2026-10-31 Phase 2 deployment validated this storage path, exact version pins and a repeat plan with no artifact/code changes. This historical evidence is separate from Phase 4 human review acceptance. See [operations](../operations.md) and [testing](../testing.md).
