Based on analysis, I can see that your code uses profanity-check library, which depends on scikit-learn internally. The joblib import error you're experiencing is a common issue  when scikit-learn tries to import joblib from sklearn.external.joblib, which was deprecated and removed in newer versions.

Here are the solutions to fix the joblib import issue:

# Solution 1: Pin compatible versions in requirements.txt

Create or update your requirements.txt with compatible versions:

  boto3>=1.34.0
  PyMySQL>=1.1.0
  profanity-check==1.0.3
  better-profanity==0.7.0
  nltk==3.8.1
  scikit-learn==1.2.2
  joblib==1.2.0
  numpy>=1.24.0,<1.25.0

# Solution 2: Update profanity-check library

  The profanity-check==1.0.3 library is outdated and may have compatibility issues. Consider
  replacing it with alt-profanity-check:

  boto3>=1.34.0
  PyMySQL>=1.1.0
  alt-profanity-check>=1.2.0
  better-profanity==0.7.0
  nltk==3.8.1
  numpy>=1.24.0
  joblib>=1.3.0

  Then update your code imports in profanity_check.py:11:
  from alt_profanity_check import predict, predict_prob

# Solution 3: Force joblib installation in Lambda layer

  If using EFS, ensure joblib is explicitly installed:

  # In your Lambda environment or EFS mount
  pip install --target /path/to/your/efs/python joblib==1.3.2
  pip install --target /path/to/your/efs/python scikit-learn==1.3.0

# Solution 4: Add joblib import fallback

  Add this to the top of your profanity_check.py after imports:

# Fix joblib import issue
  try:
      import joblib
  except ImportError:
      try:
          from sklearn.externals import joblib
      except ImportError:
          import sklearn.externals.joblib as joblib

 Recommended approach: Use Solution 2 (alt-profanity-check) as it's actively maintained and  compatible with modern scikit-learn versions. This avoids dependency issues and ensures better performance.