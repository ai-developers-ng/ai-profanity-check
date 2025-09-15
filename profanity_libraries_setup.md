# Production Content Moderation Setup Guide

## Overview

This production-ready solution provides intelligent content moderation using:

- **Profanity Libraries**: Fast, cost-effective primary detection
- **AWS Secrets Manager**: Secure database credential management
- **Selective AI**: Bedrock AI only when libraries need enhancement
- **Comprehensive Monitoring**: CloudWatch metrics and alerting
- **Error Handling**: Robust retry logic and graceful degradation

## Architecture

```
Database (MariaDB) ← Secrets Manager
        ↓
Lambda Function → Profanity Libraries → Decision Logic
        ↓                                      ↓
AI Analysis (Selective) ← Should Use AI? → Direct Decision
        ↓                                      ↓
Final Decision → Update DB + S3 Storage + SNS Alert
```

## Prerequisites

### 1. Required AWS Services
- **Lambda**: Function execution
- **Secrets Manager**: Database credentials
- **RDS/MariaDB**: Complaint storage
- **S3**: Flagged content storage
- **SNS**: Notifications
- **Bedrock**: AI analysis (optional)
- **CloudWatch**: Monitoring and logging

### 2. Database Schema

```sql
-- Add moderation columns to existing complaints table
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS moderation_status VARCHAR(50) DEFAULT NULL;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS moderation_result JSON DEFAULT NULL;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS moderation_timestamp TIMESTAMP DEFAULT NULL;
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS severity_level VARCHAR(20) DEFAULT NULL;

-- Add priority column if not exists
ALTER TABLE complaints ADD COLUMN IF NOT EXISTS priority VARCHAR(20) DEFAULT 'normal';

-- Indexes for performance
CREATE INDEX idx_moderation_processing ON complaints(status, moderation_status, created_at);
CREATE INDEX idx_moderation_status ON complaints(moderation_status, severity_level);
CREATE INDEX idx_user_history ON complaints(user_id, created_at);

-- Retry logic index
CREATE INDEX idx_retry_status ON complaints(moderation_status, created_at) 
WHERE moderation_status = 'retry';
```

## Setup Instructions

### 1. Create AWS Secrets Manager Secret

```bash
# Create database credentials secret
aws secretsmanager create-secret \
    --name "prod/content-moderation/database" \
    --description "Database credentials for content moderation system" \
    --secret-string '{
        "host": "your-mariadb-host.amazonaws.com",
        "username": "moderation_user",
        "password": "your-secure-password",
        "database": "complaints_db",
        "port": 3306
    }'

# Get the secret ARN for IAM policy
aws secretsmanager describe-secret \
    --secret-id "prod/content-moderation/database" \
    --query 'ARN' --output text
```

### 2. Create Production IAM Role

Create `production-lambda-role.json`:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```

Create `production-lambda-policy.json`:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "arn:aws:secretsmanager:*:*:secret:prod/content-moderation/database-*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel"
            ],
            "Resource": [
                "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-haiku-20240307-v1:0",
                "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-sonnet-20240229-v1:0"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "comprehend:DetectSentiment"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "sns:Publish"
            ],
            "Resource": "arn:aws:sns:*:*:content-moderation-alerts"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
                "s3:PutObjectAcl"
            ],
            "Resource": "arn:aws:s3:::your-flagged-content-bucket/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData"
            ],
            "Resource": "*"
        }
    ]
}
```

Create the role and policy:

```bash
# Create IAM role
aws iam create-role \
    --role-name ProductionContentModerationRole \
    --assume-role-policy-document file://production-lambda-role.json

# Create IAM policy
aws iam create-policy \
    --policy-name ProductionContentModerationPolicy \
    --policy-document file://production-lambda-policy.json

# Attach policy to role
aws iam attach-role-policy \
    --role-name ProductionContentModerationRole \
    --policy-arn arn:aws:iam::YOUR-ACCOUNT:policy/ProductionContentModerationPolicy

# Attach AWS managed VPC policy if database is in VPC
aws iam attach-role-policy \
    --role-name ProductionContentModerationRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole
```

### 3. Create Required S3 Bucket

```bash
# Create bucket for flagged content
aws s3 mb s3://prod-content-moderation-flagged

# Enable versioning and encryption
aws s3api put-bucket-versioning \
    --bucket prod-content-moderation-flagged \
    --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
    --bucket prod-content-moderation-flagged \
    --server-side-encryption-configuration '{
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "AES256"
                }
            }
        ]
    }'

# Set lifecycle policy for cost optimization
aws s3api put-bucket-lifecycle-configuration \
    --bucket prod-content-moderation-flagged \
    --lifecycle-configuration '{
        "Rules": [
            {
                "ID": "FlaggedContentLifecycle",
                "Status": "Enabled",
                "Filter": {"Prefix": "flagged/"},
                "Transitions": [
                    {
                        "Days": 30,
                        "StorageClass": "STANDARD_IA"
                    },
                    {
                        "Days": 90,
                        "StorageClass": "GLACIER"
                    }
                ]
            }
        ]
    }'
```

### 4. Create SNS Topic and Subscriptions

```bash
# Create SNS topic
aws sns create-topic --name content-moderation-alerts

# Subscribe email for notifications
aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:YOUR-ACCOUNT:content-moderation-alerts \
    --protocol email \
    --notification-endpoint your-team@company.com

# Subscribe to Slack webhook (optional)
aws sns subscribe \
    --topic-arn arn:aws:sns:us-east-1:YOUR-ACCOUNT:content-moderation-alerts \
    --protocol https \
    --notification-endpoint https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK
```

### 5. Build and Deploy Lambda

Create `requirements.txt`:

```txt
# Core dependencies
boto3>=1.34.0
PyMySQL>=1.1.0
cryptography>=41.0.0

# Profanity detection
profanity-check==1.0.3
better-profanity==0.7.0

# Natural language processing
nltk==3.8.1

# Performance optimization
numpy>=1.24.0
scikit-learn>=1.3.0
```

Create deployment package:

```bash
# Create deployment directory
mkdir production-deployment
cd production-deployment

# Copy your Lambda function
cp ../lambda_function.py .
cp ../requirements.txt .

# Create Lambda layer for dependencies
mkdir python
pip install -r requirements.txt -t python/

# Download NLTK data
python -c "
import nltk
import os
os.makedirs('python/nltk_data', exist_ok=True)
nltk.download('vader_lexicon', download_dir='python/nltk_data')
"

# Create layer zip
zip -r production-nlp-layer.zip python/

# Create function zip (just the code)
zip lambda-function.zip lambda_function.py

# Upload layer
aws lambda publish-layer-version \
    --layer-name production-content-moderation-layer \
    --zip-file fileb://production-nlp-layer.zip \
    --compatible-runtimes python3.9 \
    --description "Production content moderation dependencies"
```

### 6. Deploy Lambda Function

```bash
# Deploy Lambda function
aws lambda create-function \
    --function-name production-content-moderation \
    --runtime python3.9 \
    --role arn:aws:iam::YOUR-ACCOUNT:role/ProductionContentModerationRole \
    --handler lambda_function.lambda_handler \
    --zip-file fileb://lambda-function.zip \
    --timeout 300 \
    --memory-size 1024 \
    --layers arn:aws:lambda:us-east-1:YOUR-ACCOUNT:layer:production-content-moderation-layer:1 \
    --environment Variables='{
        "DB_SECRET_NAME": "prod/content-moderation/database",
        "AWS_REGION": "us-east-1",
        "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:YOUR-ACCOUNT:content-moderation-alerts",
        "FLAGGED_CONTENT_BUCKET": "prod-content-moderation-flagged",
        "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
        "AI_USAGE_THRESHOLD": "0.6",
        "SEVERITY_THRESHOLD": "3",
        "NLTK_DATA": "/opt/python/nltk_data"
    }' \
    --description "Production content moderation service" \
    --tags Environment=Production,Service=ContentModeration
```

### 7. Set Up Monitoring and Alerting

Create CloudWatch dashboard:

```bash
# Create custom metrics dashboard
aws cloudwatch put-dashboard \
    --dashboard-name "ContentModerationProduction" \
    --dashboard-body '{
        "widgets": [
            {
                "type": "metric",
                "properties": {
                    "metrics": [
                        ["AWS/Lambda", "Duration", "FunctionName", "production-content-moderation"],
                        ["AWS/Lambda", "Errors", "FunctionName", "production-content-moderation"],
                        ["AWS/Lambda", "Invocations", "FunctionName", "production-content-moderation"]
                    ],
                    "period": 300,
                    "stat": "Average",
                    "region": "us-east-1",
                    "title": "Lambda Performance"
                }
            }
        ]
    }'
```

Create CloudWatch alarms:

```bash
# Alarm for high error rate
aws cloudwatch put-metric-alarm \
    --alarm-name "ContentModeration-HighErrorRate" \
    --alarm-description "High error rate in content moderation" \
    --metric-name Errors \
    --namespace AWS/Lambda \
    --statistic Sum \
    --period 300 \
    --threshold 5 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=FunctionName,Value=production-content-moderation \
    --evaluation-periods 2 \
    --alarm-actions arn:aws:sns:us-east-1:YOUR-ACCOUNT:content-moderation-alerts

# Alarm for high duration
aws cloudwatch put-metric-alarm \
    --alarm-name "ContentModeration-HighDuration" \
    --alarm-description "Content moderation taking too long" \
    --metric-name Duration \
    --namespace AWS/Lambda \
    --statistic Average \
    --period 300 \
    --threshold 120000 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=FunctionName,Value=production-content-moderation \
    --evaluation-periods 3 \
    --alarm-actions arn:aws:sns:us-east-1:YOUR-ACCOUNT:content-moderation-alerts

# Alarm for database connection issues
aws logs put-metric-filter \
    --log-group-name "/aws/lambda/production-content-moderation" \
    --filter-name "DatabaseConnectionErrors" \
    --filter-pattern "ERROR.*database.*connection" \
    --metric-transformations \
        metricName=DatabaseConnectionErrors,metricNamespace=ContentModeration,metricValue=1
```

### 8. Set Up Scheduled Processing

Create EventBridge rule for regular processing:

```bash
# Create rule for every 5 minutes
aws events put-rule \
    --name "ContentModerationSchedule" \
    --schedule-expression "rate(5 minutes)" \
    --description "Regular content moderation processing" \
    --state ENABLED

# Add Lambda target
aws events put-targets \
    --rule ContentModerationSchedule \
    --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:YOUR-ACCOUNT:function:production-content-moderation","Input"='{"batch_size": 50, "status_filter": "pending"}'

# Grant permission
aws lambda add-permission \
    --function-name production-content-moderation \
    --statement-id allow-eventbridge \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:us-east-1:YOUR-ACCOUNT:rule/ContentModerationSchedule
```

## Production Configuration

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `DB_SECRET_NAME` | Secrets Manager secret name | - | Yes |
| `AWS_REGION` | AWS region | us-east-1 | Yes |
| `SNS_TOPIC_ARN` | SNS topic for alerts | - | Yes |
| `FLAGGED_CONTENT_BUCKET` | S3 bucket for flagged content | - | Yes |
| `BEDROCK_MODEL_ID` | Bedrock model ID | claude-3-haiku | No |
| `AI_USAGE_THRESHOLD` | When to use AI (0-1) | 0.6 | No |
| `SEVERITY_THRESHOLD` | Flagging threshold (1-10) | 3 | No |
| `NLTK_DATA` | NLTK data path | /opt/python/nltk_data | No |

### Performance Tuning

```bash
# Update Lambda configuration for high-volume processing
aws lambda update-function-configuration \
    --function-name production-content-moderation \
    --memory-size 1536 \
    --timeout 300 \
    --reserved-concurrent-executions 10

# Enable provisioned concurrency for consistent performance
aws lambda put-provisioned-concurrency-config \
    --function-name production-content-moderation \
    --provisioned-concurrency-config AllocatedProvisionedConcurrencyUnits=2
```

## Usage Examples

### 1. Manual Processing

```bash
# Process pending complaints
aws lambda invoke \
    --function-name production-content-moderation \
    --payload '{"batch_size": 100, "status_filter": "pending"}' \
    response.json

# High-accuracy mode (force AI for all)
aws lambda invoke \
    --function-name production-content-moderation \
    --payload '{"batch_size": 25, "force_ai_analysis": true}' \
    response.json

# Process specific status
aws lambda invoke \
    --function-name production-content-moderation \
    --payload '{"batch_size": 50, "status_filter": "retry"}' \
    response.json

cat response.json
```

### 2. Programmatic Invocation

```python
import boto3
import json

lambda_client = boto3.client('lambda')

# Standard processing
response = lambda_client.invoke(
    FunctionName='production-content-moderation',
    InvocationType='Event',  # Asynchronous
    Payload=json.dumps({
        'batch_size': 75,
        'status_filter': 'pending'
    })
)

# Check response
print(f"Status: {response['StatusCode']}")
if 'LogResult' in response:
    import base64
    log_data = base64.b64decode(response['LogResult']).decode('utf-8')
    print(f"Logs: {log_data}")
```

### 3. API Gateway Integration (Optional)

```bash
# Create API Gateway for external triggers
aws apigateway create-rest-api \
    --name content-moderation-api \
    --description "Content moderation API"

# Add resource and method (detailed steps needed)
# This allows HTTP triggers for processing
```

## Monitoring and Maintenance

### 1. Key Metrics to Monitor

**Lambda Metrics:**
- Duration (should be < 60 seconds for 50 complaints)
- Error rate (should be < 1%)
- Throttles (should be 0)
- Concurrent executions

**Custom Metrics:**
- Processing efficiency (% handled by libraries only)
- AI usage rate (should be 20-30%)
- Flagging rate (varies by content)
- Database connection success rate

**Business Metrics:**
- Complaints processed per hour
- Average time to moderation
- False positive rate
- Escalation rate to human review

### 2. Log Analysis Queries

```bash
# Find processing performance issues
aws logs filter-log-events \
    --log-group-name "/aws/lambda/production-content-moderation" \
    --filter-pattern "ERROR" \
    --start-time $(date -d '1 hour ago' +%s)000

# Check AI usage patterns
aws logs filter-log-events \
    --log-group-name "/aws/lambda/production-content-moderation" \
    --filter-pattern "AI.*used" \
    --start-time $(date -d '24 hours ago' +%s)000

# Monitor flagged content
aws logs filter-log-events \
    --log-group-name "/aws/lambda/production-content-moderation" \
    --filter-pattern "Flagged complaint" \
    --start-time $(date -d '24 hours ago' +%s)000
```

### 3. Database Maintenance

```sql
-- Check processing status
SELECT 
    moderation_status,
    COUNT(*) as count,
    AVG(TIMESTAMPDIFF(SECOND, created_at, moderation_timestamp)) as avg_processing_time_seconds
FROM complaints 
WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
GROUP BY moderation_status;

-- Monitor flagging rates
SELECT 
    DATE(moderation_timestamp) as date,
    COUNT(*) as total_processed,
    SUM(CASE WHEN moderation_status = 'flagged' THEN 1 ELSE 0 END) as flagged_count,
    ROUND(SUM(CASE WHEN moderation_status = 'flagged' THEN 1 ELSE 0 END) / COUNT(*) * 100, 2) as flagging_rate_percent
FROM complaints 
WHERE moderation_timestamp >= DATE_SUB(NOW(), INTERVAL 7 DAY)
GROUP BY DATE(moderation_timestamp)
ORDER BY date DESC;

-- Find retry candidates
SELECT id, user_id, created_at, moderation_status,
       JSON_EXTRACT(moderation_result, '$.error') as error_reason
FROM complaints 
WHERE moderation_status = 'retry' 
    AND created_at >= DATE_SUB(NOW(), INTERVAL 1 DAY)
ORDER BY created_at DESC;

-- Performance optimization - clean old results
DELETE FROM complaints 
WHERE moderation_timestamp < DATE_SUB(NOW(), INTERVAL 90 DAY)
    AND moderation_status IN ('approved', 'flagged');
```

## Cost Optimization

### Expected Monthly Costs (10,000 complaints)

```bash
# Cost breakdown for 10,000 complaints/month:

# Lambda Execution:
# - 10,000 invocations (batches of 50) = 200 invocations
# - 60 seconds average duration
# - Cost: ~$0.20

# Profanity Libraries:
# - 8,000 complaints (80%) processed by libraries only
# - Cost: $0 (included in Lambda)

# Bedrock AI:
# - 2,000 complaints (20%) using AI
# - 500 tokens average per call
# - Cost: 2,000 * 0.5 * $0.25/1000 = $0.25

# Other AWS Services:
# - S3 storage: ~$0.10
# - SNS notifications: ~$0.05
# - Secrets Manager: $0.40
# - CloudWatch: ~$0.20

# Total Monthly Cost: ~$1.20
# Cost per complaint: ~$0.00012
```

### Cost Optimization Strategies

```python
# 1. Implement intelligent batching
def optimize_batch_size(current_load):
    """Adjust batch size based on load"""
    if current_load > 1000:  # High load
        return 100  # Larger batches
    elif current_load > 100:  # Medium load  
        return 50   # Standard batches
    else:  # Low load
        return 25   # Smaller batches

# 2. Cache user context
def cache_user_context(user_id, context):
    """Cache frequently accessed user context"""
    # Implement Redis/ElastiCache for user context caching
    pass

# 3. Optimize AI usage
def should_use_expensive_model(complaint_data):
    """Use expensive models only for critical cases"""
    if complaint_data.get('priority') == 'urgent':
        return 'anthropic.claude-3-sonnet-20240229-v1:0'
    else:
        return 'anthropic.claude-3-haiku-20240307-v1:0'
```

## Security Best Practices

### 1. Data Protection

```bash
# Enable S3 bucket encryption
aws s3api put-bucket-encryption \
    --bucket prod-content-moderation-flagged \
    --server-side-encryption-configuration '{
        "Rules": [{
            "ApplyServerSideEncryptionByDefault": {
                "SSEAlgorithm": "aws:kms",
                "KMSMasterKeyID": "arn:aws:kms:us-east-1:YOUR-ACCOUNT:key/YOUR-KMS-KEY"
            }
        }]
    }'

# Enable CloudTrail for audit logging
aws cloudtrail create-trail \
    --name content-moderation-audit-trail \
    --s3-bucket-name your-cloudtrail-bucket \
    --include-global-service-events \
    --is-multi-region-trail
```

### 2. Access Controls

```json
{
    "Version": "2012-10-17", 
    "Statement": [
        {
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [
                "arn:aws:s3:::prod-content-moderation-flagged",
                "arn:aws:s3:::prod-content-moderation-flagged/*"
            ],
            "Condition": {
                "Bool": {
                    "aws:SecureTransport": "false"
                }
            }
        }
    ]
}
```

### 3. Secrets Rotation

```bash
# Set up automatic rotation for database credentials
aws secretsmanager update-secret \
    --secret-id prod/content-moderation/database \
    --description "Database credentials with auto-rotation" \
    --replica-regions Region=us-west-2

# Enable rotation (requires Lambda function for rotation)
aws secretsmanager rotate-secret \
    --secret-id prod/content-moderation/database \
    --rotation-lambda-arn arn:aws:lambda:us-east-1:YOUR-ACCOUNT:function:secrets-rotation \
    --rotation-rules AutomaticallyAfterDays=30
```

## Disaster Recovery

### 1. Backup Strategy

```bash
# Enable automated database backups
aws rds modify-db-instance \
    --db-instance-identifier your-db-instance \
    --backup-retention-period 7 \
    --preferred-backup-window "03:00-04:00" \
    --apply-immediately

# Cross-region S3 replication
aws s3api put-bucket-replication \
    --bucket prod-content-moderation-flagged \
    --replication-configuration '{
        "Role": "arn:aws:iam::YOUR-ACCOUNT:role/replication-role",
        "Rules": [{
            "Status": "Enabled",
            "Priority": 1,
            "Filter": {"Prefix": "flagged/"},
            "Destination": {
                "Bucket": "arn:aws:s3:::backup-content-moderation-flagged",
                "StorageClass": "STANDARD_IA"
            }
        }]
    }'
```

### 2. Recovery Procedures

```bash
# Lambda function recovery
aws lambda update-function-code \
    --function-name production-content-moderation \
    --zip-file fileb://backup-lambda-function.zip

# Database recovery from point-in-time
aws rds restore-db-instance-to-point-in-time \
    --source-db-instance-identifier your-db-instance \
    --target-db-instance-identifier your-db-instance-recovery \
    --restore-time 2024-01-15T12:00:00.000Z
```

## Testing and Validation

### 1. Integration Testing

```python
import boto3
import json
import time

def test_production_system():
    """Test production content moderation system"""
    lambda_client = boto3.client('lambda')
    
    test_cases = [
        {
            'name': 'clean_content',
            'payload': {'batch_size': 5, 'status_filter': 'pending'}
        },
        {
            'name': 'force_ai_mode',
            'payload': {'batch_size': 3, 'force_ai_analysis': True}
        }
    ]
    
    results = {}
    
    for test in test_cases:
        print(f"Running test: {test['name']}")
        
        response = lambda_client.invoke(
            FunctionName='production-content-moderation',
            InvocationType='RequestResponse',
            Payload=json.dumps(test['payload'])
        )
        
        response_payload = json.loads(response['Payload'].read())
        results[test['name']] = {
            'status_code': response['StatusCode'],
            'response': response_payload
        }
        
        print(f"Result: {response['StatusCode']}")
        time.sleep(2)  # Rate limiting
    
    return results

# Run tests
test_results = test_production_system()
print(json.dumps(test_results, indent=2))
```

### 2. Load Testing

```bash
# Simulate load with multiple concurrent invocations
for i in {1..10}; do
    aws lambda invoke \
        --function-name production-content-moderation \
        --invocation-type Event \
        --payload '{"batch_size": 20}' \
        response_$i.json &
done

wait
echo "Load test completed"
```

## Troubleshooting Guide

### Common Issues and Solutions

1. **Database Connection Timeouts**
   ```bash
   # Check VPC configuration
   aws lambda get-function-configuration \
       --function-name production-content-moderation \
       --query 'VpcConfig'
   
   # Verify security groups allow MySQL traffic
   aws ec2 describe-security-groups \
       --group-ids sg-your-lambda-sg
   ```

2. **High AI Usage Costs**
   ```python
   # Monitor AI usage pattern
   def analyze_ai_usage():
       # Query CloudWatch metrics
       # Adjust AI_USAGE_THRESHOLD if needed
       pass
   ```

3. **Processing Delays**
   ```sql
   -- Check for processing bottlenecks
   SELECT 
       COUNT(*) as pending_count,
       MIN(created_at) as oldest_pending
   FROM complaints 
   WHERE status = 'pending' AND moderation_status IS NULL;
   ```

This production setup provides a robust, scalable, and cost-effective content moderation solution with comprehensive monitoring, security, and maintenance capabilities.