import json
import boto3
import os
import pymysql
from typing import Dict, List, Any, Optional
import logging
import re
from datetime import datetime

# Profanity detection libraries
# Using alt-profanity-check (maintained fork compatible with modern scikit-learn)
from alt_profanity_check import predict, predict_prob
from better_profanity import profanity
import nltk
from nltk.sentiment import SentimentIntensityAnalyzer

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize NLTK components
try:
    nltk.data.find('vader_lexicon')
except LookupError:
    nltk.download('vader_lexicon', quiet=True)

class DatabaseConnection:
    def __init__(self):
        self.db_credentials = self._get_db_credentials()
        
    def _get_db_credentials(self) -> Dict[str, str]:
        """Retrieve database credentials from AWS Secrets Manager"""
        try:
            secret_name = os.environ.get('DB_SECRET_NAME')
            region_name = os.environ.get('AWS_REGION', 'us-east-1')
            
            if not secret_name:
                raise ValueError("DB_SECRET_NAME environment variable not set")
            
            # Create a Secrets Manager client
            session = boto3.session.Session()
            client = session.client(
                service_name='secretsmanager',
                region_name=region_name
            )
            
            # Get the secret value
            get_secret_value_response = client.get_secret_value(SecretId=secret_name)
            secret_data = json.loads(get_secret_value_response['SecretString'])
            
            logger.info(f"Successfully retrieved database credentials from secret: {secret_name}")
            
            return {
                'host': secret_data['host'],
                'username': secret_data['username'],
                'password': secret_data['password'],
                'database': secret_data['database'],
                'port': int(secret_data.get('port', 3306))
            }
            
        except Exception as e:
            logger.error(f"Error retrieving database credentials: {e}")
            raise
        
    def get_connection(self):
        """Create database connection using credentials from Secrets Manager"""
        try:
            return pymysql.connect(
                host=self.db_credentials['host'],
                user=self.db_credentials['username'],
                password=self.db_credentials['password'],
                database=self.db_credentials['database'],
                port=self.db_credentials['port'],
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
                read_timeout=10,
                write_timeout=10
            )
        except Exception as e:
            logger.error(f"Error creating database connection: {e}")
            raise

class ProductionModerationService:
    def __init__(self):
        # AWS clients
        self.bedrock_runtime = boto3.client('bedrock-runtime')
        self.comprehend = boto3.client('comprehend')
        self.sns = boto3.client('sns')
        self.s3 = boto3.client('s3')
        self.db = DatabaseConnection()
        
        # Environment variables
        self.sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        self.flagged_bucket = os.environ.get('FLAGGED_CONTENT_BUCKET')
        self.bedrock_model_id = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
        
        # Configuration
        self.ai_usage_threshold = float(os.environ.get('AI_USAGE_THRESHOLD', '0.6'))
        self.severity_threshold = int(os.environ.get('SEVERITY_THRESHOLD', '3'))
        
        # Initialize profanity detection
        self._setup_profanity_detection()
        
        # Initialize sentiment analyzer
        self.nltk_analyzer = SentimentIntensityAnalyzer()
        
        logger.info("Production moderation service initialized successfully")

    def _setup_profanity_detection(self):
        """Initialize profanity detection libraries"""
        try:
            # Load better-profanity word list
            profanity.load_censor_words()
            
            # Add common variations and misspellings if needed
            additional_words = ['fck', 'sht', 'dmn', 'btch']  # Common obfuscations
            profanity.add_censor_words(additional_words)
            
            logger.info("Profanity detection libraries configured")
            
        except Exception as e:
            logger.error(f"Error setting up profanity detection: {e}")
            raise

    def fetch_complaints_from_db(self, limit: int = 100, status: str = 'pending') -> List[Dict]:
        """Fetch unprocessed complaints from MariaDB"""
        connection = None
        try:
            connection = self.db.get_connection()
            
            with connection.cursor() as cursor:
                query = """
                SELECT 
                    id,
                    user_id,
                    complaint_xml,
                    created_at,
                    status,
                    category,
                    priority
                FROM complaints 
                WHERE status = %s 
                    AND (moderation_status IS NULL OR moderation_status = 'retry')
                ORDER BY 
                    CASE priority 
                        WHEN 'urgent' THEN 1 
                        WHEN 'high' THEN 2 
                        WHEN 'normal' THEN 3 
                        ELSE 4 
                    END,
                    created_at ASC
                LIMIT %s
                """
                
                cursor.execute(query, (status, limit))
                complaints = cursor.fetchall()
                
                logger.info(f"Fetched {len(complaints)} complaints from database")
                return complaints
                
        except Exception as e:
            logger.error(f"Error fetching complaints from database: {e}")
            return []
        finally:
            if connection:
                connection.close()

    def xml_to_json_converter(self, xml_data: str) -> Optional[Dict]:
        """
        Convert XML to JSON - Replace with your existing conversion function
        """
        try:
            import xml.etree.ElementTree as ET
            
            if not xml_data or not xml_data.strip():
                return None
                
            root = ET.fromstring(xml_data)
            
            # Enhanced XML parsing with error handling
            json_data = {
                'complaint_text': self._safe_xml_extract(root, './/complaint_text') or 
                                 self._safe_xml_extract(root, './/description') or
                                 self._safe_xml_extract(root, './/message') or '',
                'user_details': {
                    'name': self._safe_xml_extract(root, './/user_name') or '',
                    'email': self._safe_xml_extract(root, './/email') or ''
                },
                'complaint_details': {
                    'subject': self._safe_xml_extract(root, './/subject') or '',
                    'description': self._safe_xml_extract(root, './/description') or '',
                    'priority': self._safe_xml_extract(root, './/priority') or 'normal',
                    'category': self._safe_xml_extract(root, './/category') or 'general'
                }
            }
            
            return json_data
            
        except ET.ParseError as e:
            logger.error(f"XML parsing error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error converting XML to JSON: {e}")
            return None

    def _safe_xml_extract(self, root, xpath: str) -> Optional[str]:
        """Safely extract text from XML element"""
        try:
            element = root.find(xpath)
            return element.text.strip() if element is not None and element.text else None
        except Exception:
            return None

    def process_complaint_from_db(self, db_record: Dict) -> Optional[Dict]:
        """Process a single complaint record from database"""
        try:
            # Convert XML to JSON
            json_data = self.xml_to_json_converter(db_record['complaint_xml'])
            
            if not json_data:
                logger.warning(f"Failed to convert XML to JSON for complaint ID: {db_record['id']}")
                return None
            
            # Extract complaint text with fallback options
            complaint_text = self._extract_complaint_text(json_data)
            
            if not complaint_text or len(complaint_text.strip()) < 5:
                logger.warning(f"No valid complaint text found for complaint ID: {db_record['id']}")
                return None
            
            # Create processed complaint object
            processed_complaint = {
                'db_id': db_record['id'],
                'user_id': db_record['user_id'],
                'timestamp': db_record['created_at'].isoformat() if db_record['created_at'] else None,
                'category': db_record.get('category', 'general'),
                'priority': db_record.get('priority', 'normal'),
                'complaint_text': complaint_text,
                'original_json': json_data,
                'db_record': db_record
            }
            
            return processed_complaint
            
        except Exception as e:
            logger.error(f"Error processing complaint ID {db_record['id']}: {e}")
            return None

    def _extract_complaint_text(self, json_data: Dict) -> str:
        """Extract complaint text from JSON with multiple fallback options"""
        
        # Try different possible field paths
        text_paths = [
            ['complaint_text'],
            ['complaint_details', 'description'],
            ['complaint_details', 'subject'],
            ['user_details', 'message'],
            ['description'],
            ['message'],
            ['content']
        ]
        
        for path in text_paths:
            try:
                value = json_data
                for key in path:
                    value = value.get(key, {})
                
                if isinstance(value, str) and value.strip():
                    return value.strip()
                    
            except (AttributeError, TypeError):
                continue
        
        return ''

    def comprehensive_profanity_analysis(self, text: str) -> Dict[str, Any]:
        """Comprehensive profanity analysis using multiple methods"""
        results = {
            'ml_profanity_check': {},
            'dictionary_profanity': {},
            'sentiment_analysis': {},
            'text_stats': {},
            'overall_assessment': {}
        }
        
        try:
            # Text statistics
            results['text_stats'] = self._analyze_text_stats(text)
            
            # 1. ML-based profanity detection
            results['ml_profanity_check'] = self._ml_profanity_analysis(text)
            
            # 2. Dictionary-based profanity detection
            results['dictionary_profanity'] = self._dictionary_profanity_analysis(text)
            
            # 3. Sentiment analysis
            results['sentiment_analysis'] = self._sentiment_analysis(text)
            
            # 4. Overall assessment
            results['overall_assessment'] = self._calculate_overall_assessment(results)
            
        except Exception as e:
            logger.error(f"Error in comprehensive profanity analysis: {e}")
            results['error'] = str(e)
        
        return results

    def _analyze_text_stats(self, text: str) -> Dict[str, Any]:
        """Analyze basic text statistics"""
        try:
            words = text.split()
            sentences = re.split(r'[.!?]+', text)
            
            return {
                'char_count': len(text),
                'word_count': len(words),
                'sentence_count': len([s for s in sentences if s.strip()]),
                'avg_word_length': sum(len(word) for word in words) / len(words) if words else 0,
                'uppercase_ratio': sum(1 for c in text if c.isupper()) / len(text) if text else 0,
                'exclamation_count': text.count('!'),
                'question_count': text.count('?')
            }
        except Exception as e:
            logger.error(f"Error analyzing text stats: {e}")
            return {}

    def _ml_profanity_analysis(self, text: str) -> Dict[str, Any]:
        """ML-based profanity detection using profanity-check"""
        try:
            # Get probability and prediction
            profanity_prob = predict_prob([text])[0]
            is_profane = predict([text])[0] == 1
            
            return {
                'is_profane': bool(is_profane),
                'profanity_probability': float(profanity_prob),
                'confidence': float(profanity_prob) if is_profane else 1.0 - float(profanity_prob),
                'method': 'ml_based',
                'model': 'alt-profanity-check'
            }
            
        except Exception as e:
            logger.error(f"Error in ML profanity analysis: {e}")
            return {'error': str(e), 'method': 'ml_based'}

    def _dictionary_profanity_analysis(self, text: str) -> Dict[str, Any]:
        """Dictionary-based profanity detection using better-profanity"""
        try:
            has_profanity = profanity.contains_profanity(text)
            censored_text = profanity.censor(text)
            flagged_words = self._extract_flagged_words(text, censored_text)
            
            return {
                'has_profanity': has_profanity,
                'flagged_words': flagged_words,
                'word_count': len(flagged_words),
                'censored_preview': censored_text[:100] + '...' if len(censored_text) > 100 else censored_text,
                'method': 'dictionary_based',
                'library': 'better-profanity'
            }
            
        except Exception as e:
            logger.error(f"Error in dictionary profanity analysis: {e}")
            return {'error': str(e), 'method': 'dictionary_based'}

    def _sentiment_analysis(self, text: str) -> Dict[str, Any]:
        """Sentiment analysis using NLTK VADER"""
        try:
            scores = self.nltk_analyzer.polarity_scores(text)
            
            # Determine sentiment
            if scores['compound'] >= 0.05:
                sentiment = 'POSITIVE'
            elif scores['compound'] <= -0.05:
                sentiment = 'NEGATIVE'
            else:
                sentiment = 'NEUTRAL'
            
            return {
                'sentiment': sentiment,
                'compound_score': scores['compound'],
                'positive_score': scores['pos'],
                'negative_score': scores['neg'],
                'neutral_score': scores['neu'],
                'intensity': abs(scores['compound']),
                'method': 'nltk_vader'
            }
            
        except Exception as e:
            logger.error(f"Error in sentiment analysis: {e}")
            return {'error': str(e), 'method': 'nltk_vader'}

    def _extract_flagged_words(self, original: str, censored: str) -> List[str]:
        """Extract flagged words by comparing original and censored text"""
        try:
            original_words = original.split()
            censored_words = censored.split()
            
            flagged = []
            for orig, cens in zip(original_words, censored_words):
                if orig != cens and '*' in cens:
                    flagged.append(orig.strip('.,!?;:'))
            
            return list(set(flagged))  # Remove duplicates
            
        except Exception as e:
            logger.error(f"Error extracting flagged words: {e}")
            return []

    def _calculate_overall_assessment(self, analysis_results: Dict) -> Dict[str, Any]:
        """Calculate overall assessment from all analysis methods"""
        try:
            assessment = {
                'should_flag': False,
                'confidence_score': 0.0,
                'severity_level': 'LOW',
                'primary_concerns': [],
                'flagged_by_methods': [],
                'recommendation': 'APPROVE',
                'requires_ai_analysis': False
            }
            
            total_score = 0
            concerns = []
            flagged_methods = []
            
            # Analyze ML profanity results
            ml_result = analysis_results.get('ml_profanity_check', {})
            if ml_result.get('is_profane', False):
                flagged_methods.append('ML_PROFANITY')
                ml_score = ml_result.get('profanity_probability', 0) * 4  # Scale to 0-4
                total_score += ml_score
                concerns.append(f"ML detected profanity (confidence: {ml_result.get('profanity_probability', 0):.2f})")
            
            # Analyze dictionary profanity results
            dict_result = analysis_results.get('dictionary_profanity', {})
            if dict_result.get('has_profanity', False):
                flagged_methods.append('DICTIONARY_PROFANITY')
                word_penalty = min(dict_result.get('word_count', 0) * 1.5, 3)  # Max 3 points
                total_score += word_penalty
                flagged_words = dict_result.get('flagged_words', [])
                concerns.append(f"Dictionary flagged {len(flagged_words)} word(s): {flagged_words[:3]}")
            
            # Analyze sentiment
            sentiment_result = analysis_results.get('sentiment_analysis', {})
            if sentiment_result.get('sentiment') == 'NEGATIVE':
                intensity = sentiment_result.get('intensity', 0)
                if intensity > 0.6:  # Strong negative sentiment
                    flagged_methods.append('STRONG_NEGATIVE_SENTIMENT')
                    total_score += intensity * 2  # Scale negative intensity
                    concerns.append(f"Strong negative sentiment (intensity: {intensity:.2f})")
                elif intensity > 0.3:  # Moderate negative sentiment
                    flagged_methods.append('MODERATE_NEGATIVE_SENTIMENT')
                    total_score += intensity
                    concerns.append(f"Moderate negative sentiment (intensity: {intensity:.2f})")
            
            # Text pattern analysis
            text_stats = analysis_results.get('text_stats', {})
            if text_stats.get('uppercase_ratio', 0) > 0.3:  # More than 30% uppercase
                flagged_methods.append('EXCESSIVE_CAPS')
                total_score += 0.5
                concerns.append("Excessive capital letters detected")
            
            if text_stats.get('exclamation_count', 0) > 3:  # Multiple exclamations
                total_score += 0.3
                concerns.append("Multiple exclamation marks")
            
            # Final assessment
            assessment['flagged_by_methods'] = flagged_methods
            assessment['primary_concerns'] = concerns
            assessment['confidence_score'] = min(total_score / 6.0, 1.0)  # Normalize to 0-1
            
            # Determine if flagging is needed
            if total_score >= self.severity_threshold:
                assessment['should_flag'] = True
                
                if total_score >= 5:
                    assessment['severity_level'] = 'CRITICAL'
                    assessment['recommendation'] = 'ESCALATE'
                elif total_score >= 3.5:
                    assessment['severity_level'] = 'HIGH'
                    assessment['recommendation'] = 'FLAG'
                else:
                    assessment['severity_level'] = 'MEDIUM'
                    assessment['recommendation'] = 'REVIEW'
            
            # Determine if AI analysis is needed
            assessment['requires_ai_analysis'] = self._should_use_ai_analysis(assessment, analysis_results)
            
            return assessment
            
        except Exception as e:
            logger.error(f"Error calculating overall assessment: {e}")
            return {
                'should_flag': True,
                'severity_level': 'HIGH',
                'recommendation': 'REVIEW',
                'error': str(e)
            }

    def _should_use_ai_analysis(self, assessment: Dict, analysis_results: Dict) -> bool:
        """Determine if AI analysis would add value"""
        try:
            # Conditions where AI analysis is beneficial
            ai_beneficial_conditions = [
                # High confidence score (validate context)
                assessment.get('confidence_score', 0) > 0.7,
                
                # Multiple detection methods (resolve conflicts)
                len(assessment.get('flagged_by_methods', [])) >= 2,
                
                # Borderline cases (AI can provide nuanced analysis)
                0.3 <= assessment.get('confidence_score', 0) <= 0.7,
                
                # Complex sentiment patterns
                'STRONG_NEGATIVE_SENTIMENT' in assessment.get('flagged_by_methods', []) and
                len(assessment.get('flagged_by_methods', [])) == 1
            ]
            
            return any(ai_beneficial_conditions)
            
        except Exception as e:
            logger.error(f"Error determining AI analysis need: {e}")
            return False

    def analyze_with_bedrock(self, text: str, user_context: Dict = None) -> Dict[str, Any]:
        """Enhanced content analysis using Amazon Bedrock"""
        try:
            context_info = ""
            if user_context:
                context_info = f"""
User Context:
- User ID: {user_context.get('user_id', 'unknown')}
- Previous complaints: {user_context.get('complaint_count', 0)}
- Complaint category: {user_context.get('category', 'general')}
"""

            prompt = f"""
You are a professional content moderator. Analyze this customer complaint for:

1. **Toxicity Level** (0-10 scale)
2. **Threat Assessment** (NONE/LOW/MEDIUM/HIGH/CRITICAL)
3. **Content Issues** (profanity, threats, personal attacks, misinformation)
4. **Urgency Level** (LOW/MEDIUM/HIGH/CRITICAL)
5. **Recommended Action** (APPROVE/REVIEW/FLAG/ESCALATE)
6. **Summary** (brief explanation)

{context_info}

**Text to analyze:**
"{text}"

Respond in JSON format:
{{
    "toxicity_score": <0-10>,
    "threat_level": "<level>",
    "content_issues": [<list>],
    "urgency": "<level>",
    "recommended_action": "<action>",
    "summary": "<explanation>",
    "confidence": <0-1>,
    "requires_human_review": <true/false>
}}
"""

            response = self.bedrock_runtime.invoke_model(
                modelId=self.bedrock_model_id,
                contentType='application/json',
                accept='application/json',
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 800,
                    "messages": [{"role": "user", "content": prompt}]
                })
            )
            
            response_body = json.loads(response['body'].read())
            ai_content = response_body['content'][0]['text']
            
            # Extract JSON from response
            json_match = re.search(r'\{.*\}', ai_content, re.DOTALL)
            if json_match:
                ai_analysis = json.loads(json_match.group())
                return {
                    'ai_analysis': ai_analysis,
                    'model_used': self.bedrock_model_id,
                    'success': True
                }
            else:
                logger.error("Could not extract JSON from Bedrock response")
                return {'success': False, 'error': 'Invalid JSON response'}
                
        except Exception as e:
            logger.error(f"Error calling Bedrock: {e}")
            return {'success': False, 'error': str(e)}

    def get_user_context(self, user_id: str) -> Dict:
        """Get user context for analysis"""
        connection = None
        try:
            connection = self.db.get_connection()
            
            with connection.cursor() as cursor:
                query = """
                SELECT 
                    COUNT(*) as complaint_count,
                    COUNT(CASE WHEN moderation_status = 'flagged' THEN 1 END) as flagged_count,
                    MAX(created_at) as last_complaint
                FROM complaints 
                WHERE user_id = %s
                AND created_at >= DATE_SUB(NOW(), INTERVAL 90 DAY)
                """
                
                cursor.execute(query, (user_id,))
                history = cursor.fetchone()
                
                return {
                    'user_id': user_id,
                    'complaint_count': history['complaint_count'] if history else 0,
                    'flagged_count': history['flagged_count'] if history else 0,
                    'last_complaint': history['last_complaint'] if history else None
                }
                
        except Exception as e:
            logger.error(f"Error fetching user context: {e}")
            return {'user_id': user_id, 'complaint_count': 0}
        finally:
            if connection:
                connection.close()

    def enhanced_analysis(self, text: str, user_context: Dict = None) -> Dict[str, Any]:
        """Enhanced analysis combining libraries with optional AI"""
        results = {
            'text': text,
            'library_analysis': {},
            'ai_analysis': {},
            'final_decision': {},
            'processing_timestamp': datetime.utcnow().isoformat()
        }
        
        try:
            # 1. Library-based analysis
            library_results = self.comprehensive_profanity_analysis(text)
            results['library_analysis'] = library_results
            
            overall_assessment = library_results.get('overall_assessment', {})
            
            # 2. AI analysis if beneficial
            if overall_assessment.get('requires_ai_analysis', False):
                ai_results = self.analyze_with_bedrock(text, user_context)
                results['ai_analysis'] = ai_results
                
                # Combine results
                final_decision = self._combine_library_and_ai_results(overall_assessment, ai_results)
            else:
                # Use library results only
                final_decision = {
                    'should_flag': overall_assessment.get('should_flag', False),
                    'severity_level': overall_assessment.get('severity_level', 'LOW'),
                    'confidence': overall_assessment.get('confidence_score', 0.5),
                    'recommendation': overall_assessment.get('recommendation', 'APPROVE'),
                    'primary_method': 'libraries_only',
                    'reasoning': overall_assessment.get('primary_concerns', []),
                    'detection_methods': overall_assessment.get('flagged_by_methods', [])
                }
            
            results['final_decision'] = final_decision
            
        except Exception as e:
            logger.error(f"Error in enhanced analysis: {e}")
            results['error'] = str(e)
            # Fallback decision
            results['final_decision'] = {
                'should_flag': True,
                'severity_level': 'HIGH',
                'recommendation': 'REVIEW',
                'primary_method': 'error_fallback',
                'error': str(e)
            }
        
        return results

    def _combine_library_and_ai_results(self, library_assessment: Dict, ai_results: Dict) -> Dict[str, Any]:
        """Combine library and AI analysis results"""
        try:
            final_decision = {
                'should_flag': library_assessment.get('should_flag', False),
                'severity_level': library_assessment.get('severity_level', 'LOW'),
                'confidence': library_assessment.get('confidence_score', 0.5),
                'recommendation': library_assessment.get('recommendation', 'APPROVE'),
                'primary_method': 'combined_analysis',
                'reasoning': library_assessment.get('primary_concerns', []),
                'detection_methods': library_assessment.get('flagged_by_methods', [])
            }
            
            # If AI analysis was successful, use it to enhance the decision
            if ai_results.get('success') and 'ai_analysis' in ai_results:
                ai_data = ai_results['ai_analysis']
                
                # Map AI urgency to severity
                ai_severity = self._map_ai_urgency_to_severity(ai_data.get('urgency', 'LOW'))
                
                # Use AI if it suggests higher severity
                if self._severity_rank(ai_severity) > self._severity_rank(final_decision['severity_level']):
                    final_decision.update({
                        'severity_level': ai_severity,
                        'recommendation': ai_data.get('recommended_action', final_decision['recommendation']),
                        'confidence': max(final_decision['confidence'], ai_data.get('confidence', 0)),
                        'ai_insights': {
                            'toxicity_score': ai_data.get('toxicity_score', 0),
                            'threat_level': ai_data.get('threat_level', 'NONE'),
                            'content_issues': ai_data.get('content_issues', []),
                            'summary': ai_data.get('summary', ''),
                            'requires_human_review': ai_data.get('requires_human_review', False)
                        }
                    })
                    final_decision['reasoning'].append("AI analysis enhanced severity assessment")
            
            return final_decision
            
        except Exception as e:
            logger.error(f"Error combining results: {e}")
            return library_assessment

    def _map_ai_urgency_to_severity(self, urgency: str) -> str:
        """Map AI urgency to severity levels"""
        mapping = {
            'LOW': 'LOW',
            'MEDIUM': 'MEDIUM',
            'HIGH': 'HIGH',
            'CRITICAL': 'CRITICAL'
        }
        return mapping.get(urgency, 'LOW')

    def _severity_rank(self, severity: str) -> int:
        """Get numeric rank for severity comparison"""
        ranks = {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'CRITICAL': 4}
        return ranks.get(severity, 1)

    def update_moderation_status(self, complaint_id: int, status: str, analysis_result: Dict):
        """Update moderation status in database"""
        connection = None
        try:
            connection = self.db.get_connection()
            
            with connection.cursor() as cursor:
                update_query = """
                UPDATE complaints 
                SET moderation_status = %s,
                    moderation_result = %s,
                    moderation_timestamp = NOW(),
                    severity_level = %s
                WHERE id = %s
                """
                
                severity = analysis_result.get('final_decision', {}).get('severity_level', 'LOW')
                
                cursor.execute(update_query, (
                    status,
                    json.dumps(analysis_result, default=str),
                    severity,
                    complaint_id
                ))
                
                connection.commit()
                logger.info(f"Updated moderation status for complaint ID: {complaint_id}")
                
        except Exception as e:
            logger.error(f"Error updating moderation status for complaint {complaint_id}: {e}")
            raise
        finally:
            if connection:
                connection.close()

    def store_flagged_content(self, complaint_data: Dict, analysis_result: Dict) -> Optional[str]:
        """Store flagged content in S3"""
        try:
            flagged_data = {
                'db_id': complaint_data.get('db_id'),
                'user_id': complaint_data.get('user_id', 'unknown'),
                'timestamp': complaint_data.get('timestamp'),
                'category': complaint_data.get('category', 'general'),
                'priority': complaint_data.get('priority', 'normal'),
                'moderation_analysis': analysis_result,
                'flagged_at': datetime.utcnow().isoformat(),
                'complaint_preview': complaint_data.get('complaint_text', '')[:200] + '...' if len(complaint_data.get('complaint_text', '')) > 200 else complaint_data.get('complaint_text', '')
            }
            
            # Create S3 key with date partitioning for better organization
            date_prefix = datetime.utcnow().strftime('%Y/%m/%d')
            key = f"flagged/{date_prefix}/{complaint_data.get('user_id', 'unknown')}/{complaint_data.get('db_id', 'unknown')}.json"
            
            self.s3.put_object(
                Bucket=self.flagged_bucket,
                Key=key,
                Body=json.dumps(flagged_data, indent=2, default=str),
                ContentType='application/json',
                ServerSideEncryption='AES256',
                Metadata={
                    'severity': analysis_result.get('final_decision', {}).get('severity_level', 'LOW'),
                    'user_id': str(complaint_data.get('user_id', 'unknown')),
                    'flagged_date': datetime.utcnow().strftime('%Y-%m-%d')
                }
            )
            
            logger.info(f"Stored flagged content for complaint {complaint_data.get('db_id')} at {key}")
            return key
            
        except Exception as e:
            logger.error(f"Error storing flagged content: {e}")
            return None

    def send_notification(self, analysis_result: Dict, complaint_data: Dict, s3_key: str = None):
        """Send notification to internal team"""
        try:
            final_decision = analysis_result.get('final_decision', {})
            library_analysis = analysis_result.get('library_analysis', {})
            ai_analysis = analysis_result.get('ai_analysis', {})
            
            # Prepare notification message
            message = {
                'alert_type': 'CONTENT_MODERATION_ALERT',
                'alert_id': f"alert_{complaint_data.get('db_id')}_{int(datetime.utcnow().timestamp())}",
                'complaint_details': {
                    'db_id': complaint_data.get('db_id'),
                    'user_id': complaint_data.get('user_id', 'unknown'),
                    'category': complaint_data.get('category', 'general'),
                    'priority': complaint_data.get('priority', 'normal'),
                    'timestamp': complaint_data.get('timestamp')
                },
                'moderation_results': {
                    'severity_level': final_decision.get('severity_level', 'LOW'),
                    'confidence_score': final_decision.get('confidence', 0),
                    'recommendation': final_decision.get('recommendation', 'REVIEW'),
                    'primary_method': final_decision.get('primary_method', 'libraries'),
                    'detection_methods': final_decision.get('detection_methods', []),
                    'reasoning': final_decision.get('reasoning', [])
                },
                'analysis_summary': {
                    'profanity_detected': library_analysis.get('ml_profanity_check', {}).get('is_profane', False) or 
                                        library_analysis.get('dictionary_profanity', {}).get('has_profanity', False),
                    'sentiment': library_analysis.get('sentiment_analysis', {}).get('sentiment', 'UNKNOWN'),
                    'ai_analysis_used': 'ai_analysis' in analysis_result and analysis_result['ai_analysis'].get('success', False)
                },
                'action_required': {
                    'requires_immediate_attention': final_decision.get('severity_level') in ['HIGH', 'CRITICAL'],
                    'suggested_action': final_decision.get('recommendation', 'REVIEW'),
                    'human_review_required': ai_analysis.get('ai_analysis', {}).get('requires_human_review', False) if ai_analysis else False
                },
                'storage_location': s3_key,
                'generated_at': datetime.utcnow().isoformat()
            }
            
            # Create subject line based on severity
            severity_icons = {
                'LOW': '🟢',
                'MEDIUM': '🟡', 
                'HIGH': '🟠',
                'CRITICAL': '🔴'
            }
            
            severity = final_decision.get('severity_level', 'LOW')
            subject = f"{severity_icons.get(severity, '⚪')} Content Alert - {severity} - ID: {complaint_data.get('db_id')}"
            
            # Send SNS notification
            self.sns.publish(
                TopicArn=self.sns_topic_arn,
                Message=json.dumps(message, indent=2, default=str),
                Subject=subject,
                MessageAttributes={
                    'severity': {
                        'DataType': 'String',
                        'StringValue': severity
                    },
                    'complaint_id': {
                        'DataType': 'String',
                        'StringValue': str(complaint_data.get('db_id', 'unknown'))
                    },
                    'analysis_method': {
                        'DataType': 'String',
                        'StringValue': final_decision.get('primary_method', 'libraries')
                    }
                }
            )
            
            logger.info(f"Notification sent for complaint ID: {complaint_data.get('db_id')} with severity: {severity}")
            
        except Exception as e:
            logger.error(f"Error sending notification: {e}")


def lambda_handler(event, context):
    """Production-ready Lambda handler"""
    
    # Initialize metrics
    start_time = datetime.utcnow()
    metrics = {
        'processed_count': 0,
        'flagged_count': 0,
        'ai_used_count': 0,
        'error_count': 0,
        'library_only_count': 0
    }
    
    try:
        logger.info(f"Starting content moderation process with event: {json.dumps(event)}")
        
        # Initialize service
        moderation_service = ProductionModerationService()
        
        # Get processing parameters
        batch_size = event.get('batch_size', 50)
        status_filter = event.get('status_filter', 'pending')
        force_ai_analysis = event.get('force_ai_analysis', False)
        
        # Validate batch size
        if batch_size > 200:
            logger.warning(f"Batch size {batch_size} too large, limiting to 200")
            batch_size = 200
        
        # Fetch complaints
        complaints = moderation_service.fetch_complaints_from_db(
            limit=batch_size,
            status=status_filter
        )
        
        logger.info(f"Processing {len(complaints)} complaints")
        
        # Process each complaint
        for db_record in complaints:
            try:
                # Process complaint data
                complaint_data = moderation_service.process_complaint_from_db(db_record)
                
                if not complaint_data:
                    moderation_service.update_moderation_status(
                        db_record['id'], 
                        'failed_processing', 
                        {
                            'error': 'Failed to convert XML to JSON or extract complaint text',
                            'processed_at': datetime.utcnow().isoformat()
                        }
                    )
                    metrics['error_count'] += 1
                    continue
                
                # Get user context
                user_context = moderation_service.get_user_context(complaint_data['user_id'])
                
                # Perform enhanced analysis
                if force_ai_analysis:
                    # Force AI analysis for all (testing/high-accuracy mode)
                    ai_results = moderation_service.analyze_with_bedrock(
                        complaint_data['complaint_text'], 
                        user_context
                    )
                    analysis = {
                        'library_analysis': {},
                        'ai_analysis': ai_results,
                        'final_decision': moderation_service._ai_to_final_decision(ai_results),
                        'processing_timestamp': datetime.utcnow().isoformat()
                    }
                    if ai_results.get('success'):
                        metrics['ai_used_count'] += 1
                else:
                    # Standard enhanced analysis (library + selective AI)
                    analysis = moderation_service.enhanced_analysis(
                        complaint_data['complaint_text'], 
                        user_context
                    )
                    
                    # Track AI usage
                    if 'ai_analysis' in analysis and analysis['ai_analysis'].get('success'):
                        metrics['ai_used_count'] += 1
                    else:
                        metrics['library_only_count'] += 1
                
                final_decision = analysis.get('final_decision', {})
                
                # Update database with results
                status = 'flagged' if final_decision.get('should_flag', False) else 'approved'
                moderation_service.update_moderation_status(
                    db_record['id'], 
                    status, 
                    analysis
                )
                
                # Handle flagged content
                if final_decision.get('should_flag', False):
                    s3_key = moderation_service.store_flagged_content(complaint_data, analysis)
                    moderation_service.send_notification(analysis, complaint_data, s3_key)
                    metrics['flagged_count'] += 1
                    
                    logger.info(f"Flagged complaint ID {db_record['id']}: "
                               f"Method={final_decision.get('primary_method')}, "
                               f"Severity={final_decision.get('severity_level')}, "
                               f"Confidence={final_decision.get('confidence', 0):.2f}")
                
                metrics['processed_count'] += 1
                
            except Exception as e:
                logger.error(f"Error processing complaint ID {db_record['id']}: {e}")
                metrics['error_count'] += 1
                
                # Update status as error for retry
                try:
                    moderation_service.update_moderation_status(
                        db_record['id'], 
                        'retry', 
                        {
                            'error': str(e),
                            'retry_count': db_record.get('retry_count', 0) + 1,
                            'last_error_at': datetime.utcnow().isoformat()
                        }
                    )
                except Exception as update_error:
                    logger.error(f"Failed to update error status for complaint {db_record['id']}: {update_error}")
        
        # Calculate processing metrics
        end_time = datetime.utcnow()
        processing_duration = (end_time - start_time).total_seconds()
        
        # Calculate efficiency metrics
        total_processed = metrics['processed_count']
        efficiency_rate = (metrics['library_only_count'] / total_processed * 100) if total_processed > 0 else 0
        ai_usage_rate = (metrics['ai_used_count'] / total_processed * 100) if total_processed > 0 else 0
        
        # Success response
        response = {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Content moderation completed successfully',
                'processing_summary': {
                    'total_complaints_fetched': len(complaints),
                    'successfully_processed': metrics['processed_count'],
                    'flagged_for_review': metrics['flagged_count'],
                    'processing_errors': metrics['error_count']
                },
                'efficiency_metrics': {
                    'library_only_processing': metrics['library_only_count'],
                    'ai_enhanced_processing': metrics['ai_used_count'],
                    'efficiency_rate_percent': round(efficiency_rate, 1),
                    'ai_usage_rate_percent': round(ai_usage_rate, 1)
                },
                'performance_metrics': {
                    'total_processing_time_seconds': round(processing_duration, 2),
                    'avg_time_per_complaint_ms': round((processing_duration * 1000) / total_processed, 2) if total_processed > 0 else 0,
                    'throughput_per_minute': round((total_processed / processing_duration) * 60, 1) if processing_duration > 0 else 0
                },
                'configuration': {
                    'batch_size_requested': batch_size,
                    'force_ai_analysis': force_ai_analysis,
                    'bedrock_model_used': moderation_service.bedrock_model_id,
                    'severity_threshold': moderation_service.severity_threshold
                },
                'processed_at': end_time.isoformat()
            }, default=str)
        }
        
        logger.info(f"Processing completed successfully: {json.dumps(response['body'])}")
        return response
        
    except Exception as e:
        logger.error(f"Critical error in lambda_handler: {e}")
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Internal processing error',
                'error_details': str(e),
                'partial_metrics': metrics,
                'failed_at': datetime.utcnow().isoformat()
            }, default=str)
        }

    def _ai_to_final_decision(self, ai_results: Dict) -> Dict[str, Any]:
        """Convert AI results to final decision format"""
        if not ai_results.get('success'):
            return {
                'should_flag': True,
                'severity_level': 'HIGH',
                'recommendation': 'REVIEW',
                'primary_method': 'ai_error_fallback',
                'error': ai_results.get('error', 'AI analysis failed')
            }
        
        ai_data = ai_results.get('ai_analysis', {})
        
        return {
            'should_flag': ai_data.get('recommended_action', 'APPROVE') in ['FLAG', 'ESCALATE', 'REVIEW'],
            'severity_level': self._map_ai_urgency_to_severity(ai_data.get('urgency', 'LOW')),
            'confidence': ai_data.get('confidence', 0.8),
            'recommendation': ai_data.get('recommended_action', 'APPROVE'),
            'primary_method': 'ai_only',
            'reasoning': [ai_data.get('summary', 'AI analysis completed')],
            'ai_insights': ai_data
        }