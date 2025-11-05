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


# Logging Setup
logger = logging.getLogger("app.spam_detector")

# Metrics
spam_detections = Counter('spam_detections_total', 'Total spam detections')
offensive_detections = Counter('offensive_detections_total', 'Total offensive detections')


#  Paths
NLTK_DATA_PATH = os.environ.get('NLTK_DATA_PATH', '/opt/render/nltk_data')
nltk.data.path.append(NLTK_DATA_PATH)
os.makedirs(NLTK_DATA_PATH, exist_ok=True)

MODEL_DIR = os.environ.get('MODEL_DIR', '/opt/render/project/src/models')
os.makedirs(MODEL_DIR, exist_ok=True)


#  NLTK Resource Downloader
def download_nltk_resources():
    """Safely download NLTK resources."""
    try:
        nltk.download('punkt', download_dir=NLTK_DATA_PATH, quiet=True)
        nltk.download('stopwords', download_dir=NLTK_DATA_PATH, quiet=True)
        logger.info("✅ NLTK resources ready.")
    except Exception as e:
        logger.warning(f"⚠️ NLTK resources could not be downloaded: {e}")


#  Offensive Words (per language)
OFFENSIVE_WORDS = {
    "en": ["damn", "shit", "fuck", "bitch", "idiot", "stupid"],
    "lg": ["mufu", "bubi", "bwongo"],
    "rn": ["murima", "bubi"],
    "lu": ["rac", "lonyo"],
    "sw": ["mjinga", "mavi", "pumbavu"],
    "rt": ["buru", "mufu"]
}


#  Spam Detector Class
class SpamDetector:
    def __init__(self, model_path=os.path.join(MODEL_DIR, "spam_model")):
        self.model_path = model_path
        self.pipelines = {}
        self.is_loaded = False

        self.stop_words = {
            "en": self._load_stopwords('english'),
            "lg": set(),
            "rn": set(),
            "lu": set(),
            "sw": set(),
            "rt": set()
        }

        self._load_or_train_models()


    #  Helper Methods
    def _load_stopwords(self, lang: str):
        try:
            return set(stopwords.words(lang)) if lang in stopwords.fileids() else set()
        except LookupError:
            logger.warning(f"No stopwords found for {lang}.")
            return set()

    def _load_or_train_models(self):
        """Load or train spam models for all languages."""
        for lang in OFFENSIVE_WORDS.keys():
            model_file = f"{self.model_path}_{lang}.pkl"
            try:
                if os.path.exists(model_file):
                    with open(model_file, "rb") as f:
                        self.pipelines[lang] = pickle.load(f)
                    logger.info(f"✅ Loaded spam model for {lang}")
                else:
                    logger.info(f"⚙️ Training new spam model for {lang}")
                    self._train_model(lang)
            except Exception as e:
                logger.error(f"⚠️ Model loading failed for {lang}: {e}")
                self._train_model(lang)
        self.is_loaded = bool(self.pipelines)

    
    #  Model Training
    def _train_model(self, lang: str):
        """Train spam model for a given language."""
        sms_data = {
            "en": [
                ("Free entry to win a prize!", "spam"),
                ("Hello, how are you?", "ham"),
                ("You are a stupid MP!", "spam"),
                ("We need better roads in Kampala", "ham"),
                ("URGENT! Claim your reward!", "spam"),
                ("Please fix the water issue", "ham"),
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
        X = [msg for msg, _ in data]
        y = [1 if label == "spam" else 0 for _, label in data]

        try:
            stop_words = "english" if lang == "en" else None
            pipeline = Pipeline([
                ('tfidf', TfidfVectorizer(max_features=1000, stop_words=stop_words)),
                ('clf', LogisticRegression())
            ])
            pipeline.fit(X, y)

            model_file = f"{self.model_path}_{lang}.pkl"
            with open(model_file, "wb") as f:
                pickle.dump(pipeline, f)

            self.pipelines[lang] = pipeline
            logger.info(f"✅ Trained and saved spam model for {lang}")
        except Exception as e:
            logger.error(f"⚠️ Model training failed for {lang}: {e}")
            self.pipelines[lang] = None

 
    #  Preprocessing
    def preprocess_text(self, text: str, lang: str) -> str:
        try:
            text = re.sub(r'http\S+|www\S+|https\S+', '', text)
            text = re.sub(r'\s+', ' ', text)
            text = re.sub(r'[^\w\s]', '', text)
            tokens = word_tokenize(text.lower())
            stop_words = self.stop_words.get(lang, set())
            tokens = [t for t in tokens if t not in stop_words and len(t) > 2]
            return ' '.join(tokens)
        except Exception as e:
            logger.warning(f"Preprocess failed: {e}")
            return text.lower()


    #  Prediction
    def predict_spam(self, text: str, lang: str = "en") -> Tuple[bool, float]:
        if not self.is_loaded or not self.pipelines.get(lang):
            logger.warning(f"No model for {lang}. Defaulting to not spam.")
            return False, 0.0

        processed_text = self.preprocess_text(text, lang)
        try:
            pipeline = self.pipelines[lang]
            prediction = pipeline.predict([processed_text])[0]
            probability = pipeline.predict_proba([processed_text])[0][1]
            if prediction == 1:
                spam_detections.inc()
            return prediction == 1, float(probability)
        except Exception as e:
            logger.error(f"Spam prediction failed for {lang}: {e}")
            return False, 0.0

 
    #  Offensive Content Detection
    def check_offensive(self, text: str, lang: str = "en") -> bool:
        text_lower = text.lower()
        offensive_words = OFFENSIVE_WORDS.get(lang, OFFENSIVE_WORDS["en"])
        is_offensive = any(w in text_lower for w in offensive_words)
        if is_offensive:
            offensive_detections.inc()
            logger.warning(f"⚠️ Offensive content detected: {text}")
        return is_offensive
