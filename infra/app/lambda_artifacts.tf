# Upload each built package once; all functions using it share an exact S3 version.
resource "aws_s3_bucket" "lambda_artifacts" {
  bucket_prefix = substr("${local.name}-lambda-", 0, 37)
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "lambda_artifacts" {
  bucket                  = aws_s3_bucket.lambda_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lambda_artifacts" {
  bucket = aws_s3_bucket.lambda_artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "lambda_artifacts" {
  bucket = aws_s3_bucket.lambda_artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_policy" "lambda_artifacts" {
  bucket = aws_s3_bucket.lambda_artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.lambda_artifacts.arn, "${aws_s3_bucket.lambda_artifacts.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
      }, {
      Sid       = "WebFunctionImmutableRevisions"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["s3:GetObject", "s3:GetObjectVersion"]
      Resource  = "${aws_s3_bucket.lambda_artifacts.arn}/lambda/web-function.zip"
      Condition = { ArnLike = { "aws:SourceArn" = "${local.web_function_arn}/revision/*" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.lambda_artifacts]
}

resource "aws_s3_object" "lambda_artifact" {
  for_each     = toset(concat(values(local.lambda_artifacts), ["web-function.zip", "cma-preview.zip"]))
  bucket       = aws_s3_bucket.lambda_artifacts.id
  key          = "lambda/${each.key}"
  source       = "${path.module}/../../dist/lambdas/${each.key}"
  source_hash  = filesha256("${path.module}/../../dist/lambdas/${each.key}")
  content_type = "application/zip"

  depends_on = [
    aws_s3_bucket_versioning.lambda_artifacts,
    aws_s3_bucket_server_side_encryption_configuration.lambda_artifacts,
    aws_s3_bucket_policy.lambda_artifacts,
  ]
}

output "lambda_artifacts_bucket" {
  value = aws_s3_bucket.lambda_artifacts.id
}
