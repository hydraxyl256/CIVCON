# app/nltk_setup.py
import nltk
import logging

logger = logging.getLogger("app.nltk_setup")

def setup_nltk():
    try:
        nltk.download('punkt_tab', quiet=True)
        nltk.download('stopwords', quiet=True)
        logger.info("NLTK resources downloaded successfully")
    except Exception as e:
        logger.error(f"Failed to download NLTK resources: {e}")

if __name__ == "__main__":
    setup_nltk()