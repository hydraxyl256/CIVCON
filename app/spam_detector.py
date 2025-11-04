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

# -------------------------------------------------------------------
# 🧱 Logging & Metrics
# -------------------------------------------------------------------
logger = logging.getLogger("app.spam_detector")

spam_detections = Counter('spam_detections_total', 'Total spam detections')
offensive_detections = Counter('offensive_detections_total', 'Total offensive content detections')
spam_detector_failures = Counter('spam_detector_failures_total', 'Total spam detector failures')

# -------------------------------------------------------------------
# 🧰 Paths and Directories
# -------------------------------------------------------------------
NLTK_DATA_PATH = os.environ.get('NLTK_DATA_PATH', '/opt/render/nltk_data')
MODEL_DIR = os.environ.get('MODEL_DIR', '/opt/render/project/src/models')

os.makedirs(NLTK_DATA_PATH, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)

nltk.data.path.append(NLTK_DATA_PATH)

# -------------------------------------------------------------------
# 📦 NLTK Resource Setup
# -------------------------------------------------------------------
def download_nltk_resources():
    """Download NLTK resources safely (only once at build/start)."""
    try:
        nltk.download('punkt', download_dir=NLTK_DATA_PATH, quiet=True)
        nltk.download('stopwords', download_dir=NLTK_DATA_PATH, quiet=True)
        logger.info("✅ NLTK resources ready at %s", NLTK_DATA_PATH)
    except Exception as e:
        logger.warning(f"⚠️ Failed to download NLTK resources: {e}")

# -------------------------------------------------------------------
# 🚫 Offensive Words
# -------------------------------------------------------------------
OFFENSIVE_WORDS = {
    "en": ["damn", "shit", "fuck", "bitch", "idiot", "stupid", "nonsense"],
    "lg": ["mufu", "buwereza", "silu"],
    "rn": ["murima", "bubi", "okubina"],
    "lu": ["rac", "manya", "lonyo"],
    "sw": ["mjinga", "vitu vibaya", "taka"],
    "rt": ["bubi", "buru", "mufu"]
}

# -------------------------------------------------------------------
# 🧠 SpamDetector Class
# -------------------------------------------------------------------
class SpamDetector:
    """
    Robust spam and offensive content detector with built-in fault tolerance.
    Never blocks app flow — fails gracefully.
    """

    def __init__(self, model_path=os.path.join(MODEL_DIR, "spam_model")):
        self.model_path = model_path
        self.pipelines = {}
        self.is_loaded = False
        self.safe_mode = False  # Auto enabled if model or nltk fails
        self.stop_words = {
            "en": self._load_stopwords('english'),
            "lg": set(),
            "rn": set(),
            "lu": set(),
            "sw": set(),
            "rt": set()
        }

        try:
            self._load_or_train_model()
        except Exception as e:
            self.safe_mode = True
            spam_detector_failures.inc()
            logger.error(f"🚨 SpamDetector initialization failed — fallback to safe mode: {e}")

    # -------------------------------------------------------------------
    # Stopword Loader
    # -------------------------------------------------------------------
    def _load_stopwords(self, language: str) -> set:
        try:
            return set(stopwords.words(language))
        except Exception:
            logger.warning(f"Stopwords unavailable for {language}, using empty set.")
            return set()

    # -------------------------------------------------------------------
    # Model Loader / Trainer
    # -------------------------------------------------------------------
    def _load_or_train_model(self):
        for lang in OFFENSIVE_WORDS.keys():
            model_file = f"{self.model_path}_{lang}.pkl"
            try:
                if os.path.exists(model_file):
                    with open(model_file, 'rb') as f:
                        self.pipelines[lang] = pickle.load(f)
                    logger.info(f"✅ Loaded spam model for {lang}")
                else:
                    logger.info(f"⚙️ Training new spam model for {lang}")
                    self._train_model(lang)
            except Exception as e:
                spam_detector_failures.inc()
                logger.warning(f"Model load error for {lang}: {e}, retraining...")
                self._train_model(lang)
        self.is_loaded = bool(self.pipelines)

    # -------------------------------------------------------------------
    # Simple In-Memory Training (Fallback)
    # -------------------------------------------------------------------
    def _train_model(self, lang: str):
        data_samples = {
            "en": [
                ("Free entry to win a prize!", "spam"),
                ("URGENT! Claim your reward now", "spam"),
                ("You are a stupid MP!", "spam"),
                ("Hello, how are you?", "ham"),
                ("Please fix the road in Kampala", "ham"),
                ("We need water supply in my district", "ham")
            ],
            "lg": [
                ("Mufu! Okuva ewa MP!", "spam"),
                ("Wandika obuzibu bwo!", "ham")
            ],
            "rn": [
                ("Murima! MP wange!", "spam"),
                ("Amaizi g'okuzibu", "ham")
            ],
            "lu": [
                ("Rac! MP mamegi!", "spam"),
                ("Pi peke i gang", "ham")
            ],
            "sw": [
                ("Mjinga! Mbunge wako!", "spam"),
                ("Maji hayatoshi hapa", "ham")
            ],
            "rt": [
                ("Buru! MP wange!", "spam"),
                ("Amaizi g'okuzibu", "ham")
            ]
        }

        X, y = zip(*[(msg, 1 if label == "spam" else 0) for msg, label in data_samples.get(lang, data_samples["en"])])
        try:
            pipeline = Pipeline([
                ('tfidf', TfidfVectorizer(max_features=1000, stop_words=self.stop_words.get(lang, set()))),
                ('clf', LogisticRegression(max_iter=300))
            ])
            pipeline.fit(X, y)

            model_file = f"{self.model_path}_{lang}.pkl"
            with open(model_file, 'wb') as f:
                pickle.dump(pipeline, f)

            self.pipelines[lang] = pipeline
            logger.info(f"💾 Trained and saved model for {lang} at {model_file}")
        except Exception as e:
            spam_detector_failures.inc()
            logger.error(f"⚠️ Model training failed for {lang}: {e}")
            self.pipelines[lang] = None

    # -------------------------------------------------------------------
    # Text Preprocessing
    # -------------------------------------------------------------------
    def preprocess_text(self, text: str, lang: str) -> str:
        try:
            text = re.sub(r"http\S+|www\S+", "", text)
            text = re.sub(r"[^\w\s]", "", text)
            tokens = word_tokenize(text.lower())
            stop_words = self.stop_words.get(lang, set())
            tokens = [word for word in tokens if word not in stop_words and len(word) > 2]
            return " ".join(tokens)
        except Exception as e:
            logger.warning(f"Preprocessing fallback for {lang}: {e}")
            return " ".join([t for t in text.lower().split() if len(t) > 2])

    # -------------------------------------------------------------------
    # Spam Prediction
    # -------------------------------------------------------------------
    def predict_spam(self, text: str, lang: str = "en") -> Tuple[bool, float]:
        """Predict if text is spam — never raises exceptions."""
        if self.safe_mode or not self.is_loaded:
            return False, 0.0

        pipeline = self.pipelines.get(lang)
        if not pipeline:
            return False, 0.0

        try:
            processed = self.preprocess_text(text, lang)
            prediction = pipeline.predict([processed])[0]
            prob = float(pipeline.predict_proba([processed])[0][1])
            if prediction == 1:
                spam_detections.inc()
            # Avoid blocking low-confidence predictions
            return (prob >= 0.8), prob
        except Exception as e:
            spam_detector_failures.inc()
            logger.error(f"Spam prediction failed ({lang}): {e}")
            return False, 0.0

    # -------------------------------------------------------------------
    # Offensive Language Check
    # -------------------------------------------------------------------
    def check_offensive(self, text: str, lang: str = "en") -> bool:
        try:
            text_lower = text.lower()
            words = OFFENSIVE_WORDS.get(lang.lower(), OFFENSIVE_WORDS["en"])
            pattern = r'\b(?:' + '|'.join(map(re.escape, words)) + r')\b'
            if re.search(pattern, text_lower):
                offensive_detections.inc()
                logger.warning(f"🚫 Offensive content detected [{lang}]: {text}")
                return True
        except Exception as e:
            spam_detector_failures.inc()
            logger.error(f"Offensive check failed: {e}")
        return False
