locals {
  queue_name     = "${var.project}-jobs-${var.environment}"
  dlq_queue_name = "${var.project}-jobs-dlq-${var.environment}"

  common_tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  })
}

# Dead-letter queue — messages land here after max_receive_count failures
resource "aws_sqs_queue" "dlq" {
  name                      = local.dlq_queue_name
  message_retention_seconds = var.dlq_message_retention_seconds

  tags = merge(local.common_tags, {
    Name = local.dlq_queue_name
  })
}

# Main job queue with redrive policy pointing at the DLQ
resource "aws_sqs_queue" "main" {
  name                       = local.queue_name
  message_retention_seconds  = var.message_retention_seconds
  visibility_timeout_seconds = var.visibility_timeout_seconds
  receive_wait_time_seconds  = var.receive_wait_time_seconds

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = merge(local.common_tags, {
    Name = local.queue_name
  })
}

# Allow the main queue to send messages to the DLQ
resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main.arn]
  })
}
