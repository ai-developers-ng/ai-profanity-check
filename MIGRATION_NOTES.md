# Migration Notes: Dependency Updates

## Summary of Changes

Updated the project dependencies to use the latest stable versions and fixed critical compatibility issues.

## Key Changes

### 1. **Profanity Detection Library Replacement**

**Problem:** The original `profanity-check` library is no longer maintained and has a critical incompatibility with modern scikit-learn versions (>=0.24). It attempts to import `joblib` from `sklearn.externals`, which was removed.

**Solution:** Replaced with `alt-profanity-check` (maintained fork)
- ✅ Compatible with modern scikit-learn (1.6.1+)
- ✅ Drop-in replacement - same API (`predict`, `predict_prob`)
- ✅ Actively maintained and follows scikit-learn version releases
- ✅ Works with latest joblib versions

**Code Changes:**
```python
# OLD:
from profanity_check import predict, predict_prob

# NEW:
from alt_profanity_check import predict, predict_prob
```

### 2. **Updated Dependencies**

| Package | Old Version | New Version | Notes |
|---------|-------------|-------------|-------|
| boto3 | >=1.34.0 | >=1.35.80 | Latest AWS SDK |
| PyMySQL | >=1.1.0 | >=1.1.1 | Latest database driver |
| profanity-check | 1.0.3 | **REMOVED** | Incompatible with modern scikit-learn |
| alt-profanity-check | N/A | >=1.7.0.post1 | **NEW** - Maintained replacement |
| better-profanity | 0.7.0 | >=0.7.0 | Same version range |
| nltk | 3.8.1 | >=3.9.1 | Latest stable NLP toolkit |
| scikit-learn | N/A | >=1.6.1,<1.8.0 | Explicit version for compatibility |
| joblib | N/A | >=1.4.2 | Latest stable version |
| pandas | N/A | >=2.2.3 | **NEW** - Added for data processing |
| numpy | N/A | >=1.26.4,<2.1.0 | **NEW** - Required by scikit-learn |

### 3. **Python Version Compatibility**

- ✅ **Python 3.13** - Fully supported
- ✅ **Python 3.10-3.12** - Supported
- ⚠️ **Python 3.9** - May work but use Lambda Python 3.11+ runtime recommended

## Installation

### For AWS Lambda

```bash
# Create deployment package
mkdir -p production-deployment/python
cd production-deployment

# Install dependencies
pip install -r ../requirements.txt -t python/ --platform manylinux2014_x86_64 --only-binary=:all:

# Create layer
zip -r production-nlp-layer.zip python/

# Create function package
cd ..
zip lambda-function.zip profanity_check.py
```

### For Local Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Download NLTK data
python -c "import nltk; nltk.download('vader_lexicon')"
```

## Breaking Changes

### None - Drop-in Replacement

The migration from `profanity-check` to `alt-profanity-check` is a **drop-in replacement** with the same API:
- ✅ Same function signatures
- ✅ Same return values
- ✅ Same prediction logic
- ✅ No code changes required in business logic

The only changes needed are:
1. Import statement (already updated)
2. Model name in logging (already updated to 'alt-profanity-check')

## Testing Recommendations

1. **Unit Tests:** Test profanity detection with sample data
   ```python
   from alt_profanity_check import predict, predict_prob

   test_cases = [
       "This is a clean message",
       "This contains offensive content"
   ]

   for text in test_cases:
       prob = predict_prob([text])[0]
       is_profane = predict([text])[0]
       print(f"{text[:30]}... -> Profane: {is_profane}, Prob: {prob:.2f}")
   ```

2. **Integration Tests:** Test with sample complaints from database

3. **Performance Tests:** Benchmark processing time with new libraries

## Rollback Plan

If issues arise, you can temporarily pin to older compatible versions:

```txt
# Temporary rollback (not recommended for long-term)
scikit-learn==0.20.2
profanity-check==1.0.3
# Note: This limits you to Python 3.7 and has security implications
```

However, **migration to alt-profanity-check is strongly recommended** for:
- Security updates
- Python 3.10+ support
- Long-term maintenance
- AWS Lambda runtime compatibility

## Known Issues & Solutions

### Issue: ImportError with joblib

**Error:**
```
ImportError: cannot import name 'joblib' from 'sklearn.externals'
```

**Solution:** ✅ Resolved by using `alt-profanity-check`

### Issue: NumPy version conflicts

**Error:**
```
ERROR: scikit-learn X.X.X requires numpy>=1.26.4,<2.1, but you have numpy 2.1.X
```

**Solution:** ✅ Resolved by pinning numpy<2.1.0 in requirements.txt

## AWS Lambda Configuration

### Recommended Runtime Updates

- **Python 3.9** runtime → **Python 3.11** or **Python 3.12**
- This ensures compatibility with latest security patches and dependency versions

### Layer Size Considerations

The updated dependencies may increase layer size:
- scikit-learn: ~30 MB
- pandas: ~35 MB
- numpy: ~20 MB
- Total estimated: ~100-120 MB (within Lambda's 250 MB limit)

## Cost Impact

**No significant cost change expected:**
- Library processing efficiency remains the same
- AI usage patterns unchanged
- Processing time may slightly improve with newer scikit-learn optimizations

## Next Steps

1. ✅ Update requirements.txt (completed)
2. ✅ Update import statements (completed)
3. ⏳ Test in development environment
4. ⏳ Deploy to staging/test Lambda
5. ⏳ Monitor performance metrics
6. ⏳ Deploy to production

## Support & References

- [alt-profanity-check GitHub](https://github.com/dimitrismistriotis/alt-profanity-check)
- [Original profanity-check GitHub](https://github.com/vzhou842/profanity-check) (archived/unmaintained)
- [scikit-learn Documentation](https://scikit-learn.org/)
- [AWS Lambda Python Runtime](https://docs.aws.amazon.com/lambda/latest/dg/lambda-python.html)
