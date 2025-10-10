# spam_detector.py
import os
import nltk
from nltk.tokenize import word_tokenize
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
import pickle
import logging

# -------------------------------------
# Logging setup
# -------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------------
# NLTK Setup for FastAPI
# -------------------------------------
# Create or locate the nltk_data folder inside the project directory
nltk_data_dir = os.path.join(os.getcwd(), "nltk_data")
if not os.path.exists(nltk_data_dir):
    os.makedirs(nltk_data_dir)

# Add this folder to NLTK’s search paths
nltk.data.path.append(nltk_data_dir)

# ✅ Download 'punkt' tokenizer if missing
try:
    nltk.data.find("tokenizers/punkt")
    logger.info("NLTK 'punkt' already available.")
except LookupError:
    nltk.download("punkt", download_dir=nltk_data_dir)
    logger.info("Downloaded NLTK 'punkt' tokenizer to nltk_data folder")

# ✅ Download 'punkt_tab' tokenizer if missing (required for newer NLTK versions)
try:
    nltk.data.find("tokenizers/punkt_tab")
    logger.info("NLTK 'punkt_tab' already available.")
except LookupError:
    nltk.download("punkt_tab", download_dir=nltk_data_dir)
    logger.info("Downloaded NLTK 'punkt_tab' tokenizer to nltk_data folder")

# -------------------------------------
# Spam Detector Class
# -------------------------------------
class SpamDetector:
    def __init__(self):
        self.model_path = os.path.join(os.getcwd(), "spam_model.pkl")
        self.vectorizer_path = os.path.join(os.getcwd(), "tfidf_vectorizer.pkl")
        self.model = None
        self.vectorizer = None
        self._load_model()

    def _load_model(self):
        """Load pre-trained model and vectorizer if available."""
        try:
            with open(self.model_path, "rb") as f:
                self.model = pickle.load(f)
            with open(self.vectorizer_path, "rb") as f:
                self.vectorizer = pickle.load(f)
            logger.info("✅ Loaded spam detection model and vectorizer successfully.")
        except Exception as e:
            logger.warning(f"⚠️ Could not load model/vectorizer: {e}")
            self.vectorizer = TfidfVectorizer()
            self.model = MultinomialNB()
            logger.info("🧠 Initialized default dummy spam detector.")

    def preprocess_text(self, text: str) -> str:
        """Tokenize and normalize input text."""
        tokens = word_tokenize(text.lower())
        return " ".join(tokens)

    def predict_spam(self, text: str):
        """Return spam status and confidence score."""
        processed_text = self.preprocess_text(text)
        try:
            vector = self.vectorizer.transform([processed_text])
            score = self.model.predict_proba(vector)[0][1]  # Spam probability
            is_spam = score > 0.7
        except Exception:
            # If model not trained or empty vectorizer
            is_spam = False
            score = 0.0
        return is_spam, score


# -------------------------------------
# Create shared detector instance
# -------------------------------------
detector = SpamDetector()
