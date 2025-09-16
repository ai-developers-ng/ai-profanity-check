# Production Content Moderation Setup Guide - PostgreSQL

## Overview

This production-ready solution provides intelligent content moderation using:

- **PostgreSQL Database**: JSON complaint data storage
- **Profanity Libraries**: Fast, cost-effective primary detection
- **AWS Secrets Manager**: Secure database credential management
- **Selective AI**: Bedrock AI only when libraries need enhancement
- **Comprehensive Monitoring**: CloudWatch metrics and alerting
- **Error Handling**: Robust retry logic and graceful degradation

## Architecture

```
PostgreSQL Database (JSON) ← Secrets Manager
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
- **RDS PostgreSQL**: Complaint storage
- **S3**: Flagged content storage
- **SNS**: Notifications
- **Bedrock**: AI analysis (optional)
- **CloudWatch**: Monitoring and logging

### 2. PostgreSQL Database Schema

```sql
-- Create complaints table (adjust based on your existing schema)
CREATE TABLE IF NOT EXISTS complaints (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    complaint_data JSONB NOT NULL,  -- Store JSON complaint data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'pending',
    category VARCHAR(100),
    priority VARCHAR(20) DEFAULT 'normal',
    
    -- Moderation columns
    moderation_status VARCHAR(50) DEFAULT NULL,
    moderation_result JSONB DEFAULT NULL,
    moderation_timestamp TIMESTAMP DEFAULT NULL,
    severity_level VARCHAR(20) DEFAULT NULL
);

-- Create indexes for performance
CREATE INDEX idx_complaints_processing ON complaints(status, moderation_status, created_at);
CREATE INDEX idx_complaints_moderation ON complaints(moderation_status, severity_level);
CREATE INDEX idx_complaints_user_history ON complaints(user_id, created_at);
CREATE INDEX idx_complaints_retry ON complaints(moderation_status, created_at) 
WHERE moderation_status = 'retry';

-- Create index on JSON data for common queries
CREATE INDEX idx_complaints_json_text ON complaints 
USING gin ((complaint_data->'complaint_text'));

-- Add updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_complaints_updated_at 
BEFORE UPDATE ON complaints 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Example of expected JSON structure in complaint_data column:
/*
{
    "complaint_text": "I'm having issues with your platform...",
    "user_details": {
        "name": "John Doe",
        "email": "john@example.com"
    },
    "complaint_details": {
        "subject": "Platform Issue",
        "description": "Detailed description...",
        "category": "technical"
    },
    "metadata": {
        "source": "web",
        "ip_address": "192.168.1.1",
        "user_agent": "Mozilla/5.0..."
    }
}
*/
```

## Setup Instructions

### 1. Create AWS Secrets Manager Secret

```bash
# Create PostgreSQL credentials secret
aws secretsmanager create-secret \
    --name "prod/content-moderation/postgres" \
    --description "PostgreSQL credentials for content moderation system" \
    --secret-string '{
        "host": "your-postgres-host.amazonaws.com",
        "username": "moderation_user",
        "password": "your-secure-password",
        "database": "complaints_db",
        "port": 5432
    }'

# Get the secret ARN for IAM policy
aws secretsmanager describe-secret \
    --secret-id "prod/content-moderation/postgres" \
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
            "Resource": "arn:aws:secretsmanager:*:*:secret:prod/content-moderation/postgres-*"
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

### 3. Build and Deploy Lambda

Create `requirements.txt`:

```txt
# Core dependencies
boto3>=1.34.0
psycopg2-binary>=2.9.7

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

### 4. Deploy Lambda Function

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
        "DB_SECRET_NAME": "prod/content-moderation/postgres",
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

### 5. Set Up PostgreSQL RDS Instance (if needed)

```bash
# Create PostgreSQL RDS instance
aws rds create-db-instance \
    --db-instance-identifier prod-content-moderation-db \
    --db-instance-class db.t3.micro \
    --engine postgres \
    --engine-version 15.3 \
    --master-username postgres \
    --master-user-password YourSecurePassword123! \
    --allocated-storage 20 \
    --storage-type gp2 \
    --vpc-security-group-ids sg-your-security-group \
    --db-subnet-group-name your-db-subnet-group \
    --backup-retention-period 7 \
    --multi-az \
    --storage-encrypted \
    --deletion-protection

# Create database for complaints
# Connect to PostgreSQL and run:
# CREATE DATABASE complaints_db;
# CREATE USER moderation_user WITH PASSWORD 'your-secure-password';
# GRANT ALL PRIVILEGES ON DATABASE complaints_db TO moderation_user;
```

## JSON Data Structure Examples

### Expected complaint_data JSON structure:

```json
{
    "complaint_text": "I'm having issues with your platform and it's really frustrating!",
    "user_details": {
        "name": "John Smith",
        "email": "john.smith@example.com",
        "phone": "+1-555-0123"
    },
    "complaint_details": {
        "subject": "Platform Performance Issues",
        "description": "The platform has been slow and unresponsive for the past week",
        "category": "technical",
        "subcategory": "performance",
        "severity": "medium"
    },
    "metadata": {
        "source": "web_form",
        "ip_address": "192.168.1.100",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "referrer": "https://support.example.com",
        "session_id": "sess_abc123def456",
        "form_version": "v2.1"
    },
    "attachments": [
        {
            "filename": "screenshot.png",
            "url": "s3://uploads/complaints/screenshot.png",
            "type": "image"
        }
    ],
    "timestamps": {
        "submitted_at": "2024-01-15T10:30:00Z",
        "updated_at": "2024-01-15T10:30:00Z"
    }
}
```

### Simplified JSON structure (minimum required):

```json
{
    "complaint_text": "Your service is terrible and I want a refund!",
    "message": "Alternative field for complaint text",
    "description": "Another alternative field"
}
```

## Database Operations

### Insert Sample Data

```sql
-- Insert sample complaints for testing
INSERT INTO complaints (user_id, complaint_data, category, priority) VALUES 
(
    'user123',
    '{
        "complaint_text": "I am very frustrated with your platform. It keeps crashing!",
        "user_details": {"name": "Test User", "email": "test@example.com"},
        "complaint_details": {"subject": "Platform Issues", "category": "technical"}
    }',
    'technical',
    'high'
),
(
    'user456', 
    '{
        "complaint_text": "Great service, very satisfied with the support team.",
        "user_details": {"name": "Happy User", "email": "happy@example.com"},
        "complaint_details": {"subject": "Positive Feedback", "category": "feedback"}
    }',
    'feedback',
    'normal'
),
(
    'user789',
    '{
        "complaint_text": "This platform is absolute garbage! What a scam!",
        "user_details": {"name": "Angry User", "email": "angry@example.com"},
        "complaint_details": {"subject": "Terrible Experience", "category": "complaint"}
    }',
    'complaint',
    'urgent'
);
```

### Monitor Processing Results

```sql
-- Check moderation status
SELECT 
    moderation_status,
    COUNT(*) as count,
    ROUND(AVG(EXTRACT(EPOCH FROM (moderation_timestamp - created_at))), 2) as avg_processing_time_seconds
FROM complaints 
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY moderation_status;

-- View flagged complaints
SELECT 
    id,
    user_id,
    complaint_data->>'complaint_text' as complaint_text,
    severity_level,
    moderation_status,
    moderation_result->'final_decision'->>'primary_method' as analysis_method,
    moderation_result->'final_decision'->>'confidence' as confidence_score,
    created_at,
    moderation_timestamp
FROM complaints 
WHERE moderation_status = 'flagged'
ORDER BY moderation_timestamp DESC
LIMIT 10;

-- Performance analysis
SELECT 
    DATE(moderation_timestamp) as date,
    COUNT(*) as total_processed,
    COUNT(CASE WHEN moderation_status = 'flagged' THEN 1 END) as flagged_count,
    COUNT(CASE WHEN moderation_result->'ai_analysis'->>'success' = 'true' THEN 1 END) as ai_used_count,
    ROUND(AVG(CAST(moderation_result->'final_decision'->>'confidence' AS FLOAT)), 3) as avg_confidence
FROM complaints 
WHERE moderation_timestamp >= NOW() - INTERVAL '7 days'
    AND moderation_status IS NOT NULL
GROUP BY DATE(moderation_timestamp)
ORDER BY date DESC;

-- Find retry candidates
SELECT id, user_id, created_at, 
       moderation_result->>'error' as error_reason,
       (moderation_result->>'retry_count')::int as retry_count
FROM complaints 
WHERE moderation_status = 'retry' 
    AND created_at >= NOW() - INTERVAL '1 day'
ORDER BY created_at DESC;
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

# Process retry queue
aws lambda invoke \
    --function-name production-content-moderation \
    --payload '{"batch_size": 50, "status_filter": "retry"}' \
    response.json

cat response.json
```

### 2. Programmatic Integration

```python
import boto3
import json
import psycopg2
import psycopg2.extras

# Insert new complaint and trigger processing
def submit_complaint_for_moderation(user_id, complaint_data):
    # Database connection (use your actual credentials)
    conn = psycopg2.connect(
        host="your-postgres-host",
        database="complaints_db", 
        user="moderation_user",
        password="your-password"
    )
    
    try:
        with conn.cursor() as cursor:
            # Insert complaint
            insert_query = """
            INSERT INTO complaints (user_id, complaint_data, category, priority)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """
            
            cursor.execute(insert_query, (
                user_id,
                json.dumps(complaint_data),
                complaint_data.get('complaint_details', {}).get('category', 'general'),
                complaint_data.get('complaint_details', {}).get('priority', 'normal')
            ))
            
            complaint_id = cursor.fetchone()[0]
            conn.commit()
            
            print(f"Complaint {complaint_id} submitted for moderation")
            
            # Trigger Lambda processing
            lambda_client = boto3.client('lambda')
            response = lambda_client.invoke(
                FunctionName='production-content-moderation',
                InvocationType='Event',
                Payload=json.dumps({'batch_size': 10})
            )
            
            return complaint_id
            
    finally:
        conn.close()

# Example usage
complaint_data = {
    "complaint_text": "I'm experiencing issues with your service",
    "user_details": {
        "name": "John Doe",
        "email": "john@example.com"
    },
    "complaint_details": {
        "subject": "Service Issue",
        "category": "technical",
        "priority": "normal"
    }
}

complaint_id = submit_complaint_for_moderation("user123", complaint_data)
```

## Performance Optimization

### 1. Database Optimization

```sql
-- Create partial indexes for better performance
CREATE INDEX idx_complaints_pending 
ON complaints (created_at) 
WHERE status = 'pending' AND moderation_status IS NULL;

CREATE INDEX idx_complaints_flagged 
ON complaints (moderation_timestamp DESC) 
WHERE moderation_status = 'flagged';

-- Optimize JSON queries
CREATE INDEX idx_complaint_text_gin 
ON complaints USING gin ((complaint_data->'complaint_text'));

-- Analyze table statistics
ANALYZE complaints;
```

### 2. Lambda Optimization

```bash
# Update Lambda for better performance
aws lambda update-function-configuration \
    --function-name production-content-moderation \
    --memory-size 1536 \
    --timeout 300 \
    --reserved-concurrent-executions 10

# Enable provisioned concurrency
aws lambda put-provisioned-concurrency-config \
    --function-name production-content-moderation \
    --provisioned-concurrency-config AllocatedProvisionedConcurrencyUnits=2
```

## Monitoring and Troubleshooting

### PostgreSQL-Specific Monitoring

```sql
-- Monitor connection usage
SELECT 
    state,
    COUNT(*) as connection_count
FROM pg_stat_activity 
WHERE datname = 'complaints_db'
GROUP BY state;

-- Check for long-running queries
SELECT 
    pid,
    now() - pg_stat_activity.query_start AS duration,
    query 
FROM pg_stat_activity 
WHERE (now() - pg_stat_activity.query_start) > interval '5 minutes'
    AND state = 'active';

-- Monitor table size and growth
SELECT 
    schemaname,
    tablename,
    attname,
    n_distinct,
    correlation 
FROM pg_stats 
WHERE tablename = 'complaints';
```

### Application-Specific Queries

```sql
-- Find JSON parsing issues
SELECT id, user_id, created_at,
       CASE 
           WHEN complaint_data IS NULL THEN 'NULL complaint_data'
           WHEN jsonb_typeof(complaint_data) != 'object' THEN 'Invalid JSON type'
           WHEN complaint_data->>'complaint_text' IS NULL THEN 'Missing complaint_text'
           ELSE 'OK'
       END as issue_type
FROM complaints 
WHERE moderation_status = 'failed_processing'
    AND created_at >= NOW() - INTERVAL '24 hours';

-- Analyze AI usage patterns
SELECT 
    DATE(moderation_timestamp) as date,
    COUNT(*) as total,
    COUNT(CASE WHEN moderation_result->'ai_analysis'->>'success' = 'true' THEN 1 END) as ai_used,
    ROUND(
        COUNT(CASE WHEN moderation_result->'ai_analysis'->>'success' = 'true' THEN 1 END)::FLOAT / 
        COUNT(*)::FLOAT * 100, 2
    ) as ai_usage_percent
FROM complaints 
WHERE moderation_timestamp >= NOW() - INTERVAL '30 days'
    AND moderation_status IS NOT NULL
GROUP BY DATE(moderation_timestamp)
ORDER BY date DESC;
```

## Cost Analysis

### Expected Monthly Costs (10,000 complaints)

- **Lambda Execution**: ~$0.20
- **Profanity Libraries**: $0 (included)
- **Bedrock AI (20% usage)**: ~$0.25
- **PostgreSQL RDS**: ~$13 (db.t3.micro)
- **S3 Storage**: ~$0.10
- **Secrets Manager**: $0.40
- **Total**: ~$14/month (~$0.0014 per complaint)

### Cost Optimization

1. **Use Aurora Serverless** for variable workloads
2. **Implement connection pooling** to reduce database connections
3. **Archive old complaints** to reduce storage costs
4. **Optimize AI usage** through better thresholds

This setup provides a robust, production-ready content moderation system optimized for PostgreSQL with JSON data storage, eliminating the need for XML-to-JSON conversion while maintaining all advanced features.