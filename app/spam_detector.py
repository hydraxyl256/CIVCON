import pickle
import re
import os
from typing import Tuple
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
import logging
from prometheus_client import Counter

# Setup logging
logger = logging.getLogger("app.spam_detector")

# Metrics
spam_detections = Counter('spam_detections_total', 'Total spam detections')
offensive_detections = Counter('offensive_detections_total', 'Total offensive content detections')

# Set NLTK data path for Render (persistent directory)
NLTK_DATA_PATH = os.environ.get('NLTK_DATA_PATH', '/opt/render/nltk_data')
nltk.data.path.append(NLTK_DATA_PATH)
os.makedirs(NLTK_DATA_PATH, exist_ok=True)

# Ensure model directory is writable
MODEL_DIR = os.environ.get('MODEL_DIR', '/opt/render/project/src/models')
os.makedirs(MODEL_DIR, exist_ok=True)

# Download NLTK resources (called at build time)
def download_nltk_resources():
    """Download NLTK resources. Call this at build time."""
    try:
        nltk.download('punkt_tab', download_dir=NLTK_DATA_PATH, quiet=True, timeout=60)
        nltk.download('stopwords', download_dir=NLTK_DATA_PATH, quiet=True, timeout=60)
        logger.info("NLTK resources downloaded successfully to %s", NLTK_DATA_PATH)
    except Exception as e:
        logger.error(f"Failed to download NLTK resources: {e}")

# Expanded offensive words list (extend further for production)
OFFENSIVE_WORDS = {
    "en": ["damn", "shit", "fuck", "bitch", "idiot", "stupid"],
    "lg": ["kibadde", "buwereza", "mufu"],
    "rn": ["bubi", "okubina", "murima"],
    "lu": ["manya", "lonyo", "rac"],
    "sw": ["vitu vibaya", "kamwaga", "mjinga"],
    "rt": ["bubi", "buru", "mufu"]
}

class SpamDetector:
    def __init__(self, model_path=os.path.join(MODEL_DIR, "spam_model")):
        self.model_path = model_path
        self.pipelines = {}  # Store pipelines per language
        self.is_loaded = False
        self.stop_words = {
            "en": self._load_stopwords('english'),
            "lg": set(),  # Add Luganda stopwords if available
            "rn": set(),  # Add Runyankore stopwords
            "lu": set(),  # Add Lango stopwords
            "sw": set(),  # Add Swahili stopwords
            "rt": set()   # Add Rutooro stopwords
        }
        self._load_or_train_model()

    def _load_stopwords(self, language: str) -> set:
        """Load stopwords for a language with error handling."""
        try:
            return set(stopwords.words(language)) if language in stopwords.fileids() else set()
        except LookupError:
            logger.warning(f"Stopwords for {language} not found. Using empty set.")
            return set()

    def _load_or_train_model(self):
        """Load or train models for each language."""
        for lang in OFFENSIVE_WORDS.keys():
            model_file = f"{self.model_path}_{lang}.pkl"
            try:
                if os.path.exists(model_file):
                    with open(model_file, 'rb') as f:
                        self.pipelines[lang] = pickle.load(f)
                    logger.info(f"Loaded spam model for {lang} from {model_file}")
                else:
                    logger.info(f"No model file found for {lang} at {model_file}. Training new model.")
                    self._train_model(lang)
            except Exception as e:
                logger.error(f"Failed to load model for {lang} from {model_file}: {e}")
                self._train_model(lang)
        self.is_loaded = bool(self.pipelines)

    def _train_model(self, lang: str):
        """Train a spam detection model for a specific language."""
        sms_data = {
            "en": [
                ("Free entry to win a prize!", "spam"),
                ("Hello, how are you?", "ham"),
                ("You are a stupid MP!", "spam"),
                ("We need better roads in Kampala", "ham"),
                ("URGENT! Claim your reward!", "spam"),
                ("Please fix the water issue", "ham")
            ],
            "lg": [
                ("Wandika obuzibu bwo!", "ham"),
                ("Mufu! Okuva ewa MP!", "spam"),
                ("Amazzi ga wano gali mabi", "ham")
            ],
            "rn": [
                ("Okwanjwa ku buzibu!", "ham"),
                ("Murima! MP wange!", "spam"),
                ("Amaizi g'okuzibu", "ham")
            ],
            "lu": [
                ("Wek ayie gi MP!", "ham"),
                ("Rac! MP mamegi!", "spam"),
                ("Pi peke i gang", "ham")
            ],
            "sw": [
                ("Toa hoja zako!", "ham"),
                ("Mjinga! Mbunge wako!", "spam"),
                ("Maji hayatoshi hapa", "ham")
            ],
            "rt": [
                ("Andika ebizibu byo!", "ham"),
                ("Buru! MP wange!", "spam"),
                ("Amaizi g'okuzibu", "ham")
            ]
        }

        data = sms_data.get(lang, sms_data["en"])
        X, y = [], []
        for msg, label in data:
            X.append(msg)
            y.append(1 if label.lower() == 'spam' else 0)

        if not X or not y:
            logger.warning(f"No training data for {lang}. Skipping model training.")
            self.pipelines[lang] = None
            return

        pipeline = Pipeline([
            ('tfidf', TfidfVectorizer(max_features=1000, stop_words=self.stop_words.get(lang, set()))),
            ('clf', LogisticRegression())
        ])
        try:
            pipeline.fit(X, y)
            model_file = f"{self.model_path}_{lang}.pkl"
            with open(model_file, 'wb') as f:
                pickle.dump(pipeline, f)
            self.pipelines[lang] = pipeline
            logger.info(f"Trained and saved spam model for {lang} to {model_file}")
        except Exception as e:
            logger.error(f"Failed to train or save model for {lang}: {e}")
            self.pipelines[lang] = None

    def preprocess_text(self, text: str, lang: str) -> str:
        """Preprocess text for spam/offensive detection."""
        try:
            text = re.sub(r'http\S+|www\S+|https\S+', '', text, flags=re.MULTILINE)
            text = re.sub(r'\s+', ' ', text)
            text = re.sub(r'[^\w\s]', '', text)
            tokens = word_tokenize(text.lower())
            stop_words = self.stop_words.get(lang, set())
            tokens = [word for word in tokens if word not in stop_words and len(word) > 2]
            return ' '.join(tokens)
        except LookupError:
            logger.error(f"Tokenization failed: NLTK punkt resource not found. Using fallback.")
            tokens = text.lower().split()
            stop_words = self.stop_words.get(lang, set())
            tokens = [word for word in tokens if word not in stop_words and len(word) > 2]
            return ' '.join(tokens)
        except Exception as e:
            logger.error(f"Text preprocessing failed: {e}")
            tokens = text.lower().split()
            stop_words = self.stop_words.get(lang, set())
            tokens = [word for word in tokens if word not in stop_words and len(word) > 2]
            return ' '.join(tokens)

    def predict_spam(self, text: str, lang: str = "en") -> Tuple[bool, float]:
        """Predict if text is spam. Returns (is_spam, probability)."""
        if not self.is_loaded or not self.pipelines.get(lang):
            logger.warning(f"No model for {lang}. Returning default 'not spam'.")
            return False, 0.0

        processed_text = self.preprocess_text(text, lang)
        try:
            pipeline = self.pipelines[lang]
            prediction = pipeline.predict([processed_text])[0]
            probability = pipeline.predict_proba([processed_text])[0][1]
            if prediction == 1:
                spam_detections.inc()
            return prediction == 1, probability
        except Exception as e:
            logger.error(f"Prediction failed for {lang}: {e}")
            return False, 0.0

    def check_offensive(self, text: str, lang: str = "en") -> bool:
        """Check if text contains offensive words for the given language."""
        text_lower = text.lower()
        offensive_list = OFFENSIVE_WORDS.get(lang.lower(), OFFENSIVE_WORDS["en"])
        is_offensive = any(word in text_lower for word in offensive_list)
        if is_offensive:
            offensive_detections.inc()
            logger.warning(f"Offensive content detected in {lang}: {text}")
        return is_offensive