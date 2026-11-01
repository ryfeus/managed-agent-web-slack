# Phase 3 retires native trajectories without deleting any historical objects or
# their privacy controls. A later operator may explicitly retire the physical bucket.
removed {
  from = aws_s3_bucket.openappa_snapshots
  lifecycle { destroy = false }
}

removed {
  from = aws_s3_bucket_public_access_block.openappa_snapshots
  lifecycle { destroy = false }
}

removed {
  from = aws_s3_bucket_server_side_encryption_configuration.openappa_snapshots
  lifecycle { destroy = false }
}

removed {
  from = aws_s3_bucket_policy.openappa_snapshots
  lifecycle { destroy = false }
}

# The obsolete scheduler inline IAM policy is intentionally deleted. The existing
# deploy guard still requires review of that IAM deletion; do not bypass the guard.
