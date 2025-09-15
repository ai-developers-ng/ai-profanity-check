# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Production-ready AWS Lambda-based content moderation system that intelligently combines profanity libraries with selective AI analysis for cost-effective content filtering. The system processes customer complaints stored in MariaDB and flags inappropriate content using a tiered analysis approach.

## Architecture

**Core Components:**
- `profanity_check.py` - Main Lambda function implementing the moderation service
- AWS Lambda runtime with Python 3.9
- MariaDB database for complaint storage with moderation columns
- AWS Bedrock (Claude models) for enhanced AI analysis
- AWS Secrets Manager for secure database credentials
- S3 for flagged content storage
- SNS for alert notifications

**Processing Flow:**
1. Library-based profanity detection (primary, cost-effective)
2. Sentiment analysis and text pattern recognition
3. Decision logic determines if AI analysis adds value
4. Selective Bedrock AI analysis for complex cases
5. Combined decision and database/S3 storage

## Key Dependencies

Based on `requirements.txt` mentioned in README:
```
boto3>=1.34.0
PyMySQL>=1.1.0
profanity-check==1.0.3
better-profanity==0.7.0
nltk==3.8.1
```

## Development Commands

**Lambda Deployment:**
```bash
# Create deployment package
mkdir production-deployment && cd production-deployment
pip install -r requirements.txt -t python/
zip -r production-nlp-layer.zip python/
zip lambda-function.zip profanity_check.py

# Deploy with AWS CLI
aws lambda create-function --function-name production-content-moderation ...
```

**Testing:**
```bash
# Manual invocation
aws lambda invoke --function-name production-content-moderation \
  --payload '{"batch_size": 10, "status_filter": "pending"}' response.json

# High-accuracy mode
aws lambda invoke --function-name production-content-moderation \
  --payload '{"batch_size": 5, "force_ai_analysis": true}' response.json
```

**Database Operations:**
```sql
-- Check processing status
SELECT moderation_status, COUNT(*) FROM complaints
WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
GROUP BY moderation_status;

-- Monitor flagging rates
SELECT DATE(moderation_timestamp) as date,
       COUNT(*) as total,
       SUM(CASE WHEN moderation_status = 'flagged' THEN 1 ELSE 0 END) as flagged
FROM complaints
WHERE moderation_timestamp >= DATE_SUB(NOW(), INTERVAL 7 DAY)
GROUP BY DATE(moderation_timestamp);
```

## Code Architecture

**Core Classes:**
- `DatabaseConnection` - Handles MariaDB connections via Secrets Manager
- `ProductionModerationService` - Main orchestration class with methods:
  - `comprehensive_profanity_analysis()` - Multi-library profanity detection
  - `analyze_with_bedrock()` - AI analysis using Claude models
  - `enhanced_analysis()` - Combined library + selective AI approach
  - `update_moderation_status()` - Database persistence

**Decision Logic:**
- Libraries handle 70-80% of content (profanity-check, better-profanity, NLTK sentiment)
- AI analysis triggered only for borderline/complex cases (`ai_usage_threshold`)
- Severity scoring combines multiple detection methods
- Configurable thresholds for flagging decisions

**Error Handling:**
- Graceful degradation when AI services unavailable
- Retry logic for database connection issues
- Fallback decisions ensure no content bypasses moderation

## Environment Configuration

**Required Environment Variables:**
- `DB_SECRET_NAME` - Secrets Manager secret for database credentials
- `SNS_TOPIC_ARN` - Alert notifications topic
- `FLAGGED_CONTENT_BUCKET` - S3 bucket for storing flagged content
- `BEDROCK_MODEL_ID` - AI model (default: claude-3-haiku)
- `AI_USAGE_THRESHOLD` - When to use AI (0-1, default: 0.6)
- `SEVERITY_THRESHOLD` - Flagging threshold (1-10, default: 3)

## Database Schema

The system expects these additional columns in the `complaints` table:
```sql
moderation_status VARCHAR(50) -- 'pending', 'approved', 'flagged', 'retry'
moderation_result JSON        -- Complete analysis results
moderation_timestamp TIMESTAMP
severity_level VARCHAR(20)    -- 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
priority VARCHAR(20)          -- Processing priority
```

## Cost Optimization

- **Library-first approach**: 80% processed without AI costs
- **Selective AI usage**: Only 20-30% use expensive Bedrock calls
- **Intelligent batching**: Adjustable batch sizes based on load
- **Expected cost**: ~$0.00012 per complaint at scale

## Monitoring

**Key Metrics:**
- Processing efficiency (% library-only vs AI-enhanced)
- Average processing time per complaint
- Flagging rate and false positive tracking
- Database connection success rate

**CloudWatch Alarms:**
- High error rate (>5 errors in 10 minutes)
- High duration (>120 seconds average)
- Database connection failures

## Security Notes

- Database credentials stored in AWS Secrets Manager
- S3 encryption enabled for flagged content storage
- IAM roles with least-privilege access
- VPC configuration for database access
- CloudTrail audit logging enabled