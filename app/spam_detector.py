# spam_detector.py
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

# NLTK setup
nltk.download('stopwords', quiet=True)
nltk.download('punkt', quiet=True)

# Example offensive words list (extendable per language)
OFFENSIVE_WORDS = {
    "en": ["damn", "shit", "fuck", "bitch"],
    "lg": ["kibadde", "buwereza"],
    "rk": ["bubi", "okubina"],  # Runyankole
    "ach": ["manya", "lonyo"],   # Luo/Acholi
    "sw": ["vitu vibaya", "kamwaga"],  # Swahili
    "rt": ["bubi", "buru"]       # Rutoro
}


class SpamDetector:
    def __init__(self):
        self.model_path = "spam_model.pkl"
        self.pipeline: Pipeline = None
        self.is_loaded = False
        self._load_model()

    def _load_model(self):
        """Load model from disk or train a new one if not found."""
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, 'rb') as f:
                    self.pipeline = pickle.load(f)
                self.is_loaded = True
                print("✅ Spam detection model loaded successfully.")
            except Exception as e:
                print(f"⚠️ Failed to load model: {e}. Training a new model.")
                self._train_model()
        else:
            print("⚠️ Model not found. Training a new model.")
            self._train_model()

    def _train_model(self):
        """Train a simple spam detection model with sample data."""
        sms_data = [
            ("Free entry in 2 a wkly comp to win FA Cup final tkts...", "spam"),
            ("K..give me a sec... I'll call u right now..", "ham"),
            ("Ok i will come soon", "ham"),
            ("URGENT! You won a £1000 prize ...", "spam"),
            ("Congrats! 1 year special cinema ticket for 2 is yours.", "spam"),
            ("Hello my love, what are you doing? I miss you.", "ham"),
            ("Your free ringtone is waiting. Call 08702344776 now!", "spam"),
            ("I love you too! Can't wait to see you.", "ham")
        ]

        X, y = [], []
        for row in sms_data:
            if isinstance(row, (tuple, list)) and len(row) == 2:
                msg, label = row
                X.append(msg)
                y.append(1 if label.lower() == 'spam' else 0)

        if not X or not y:
            print("⚠️ No valid training data found. Model will not be trained.")
            self.pipeline = None
            self.is_loaded = False
            return

        self.pipeline = Pipeline([
            ('tfidf', TfidfVectorizer(max_features=1000, stop_words='english')),
            ('clf', LogisticRegression())
        ])
        self.pipeline.fit(X, y)

        try:
            with open(self.model_path, 'wb') as f:
                pickle.dump(self.pipeline, f)
            self.is_loaded = True
            print("✅ Spam detection model trained and saved successfully.")
        except Exception as e:
            print(f"⚠️ Failed to save model: {e}")

    def preprocess_text(self, text: str) -> str:
        """Preprocess text for spam/offensive detection."""
        text = re.sub(r'http\S+|www\S+|https\S+', '', text, flags=re.MULTILINE)
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'[^\w\s]', '', text)
        tokens = word_tokenize(text.lower())
        stop_words = set(stopwords.words('english'))
        tokens = [word for word in tokens if word not in stop_words and len(word) > 2]
        return ' '.join(tokens)

    def predict_spam(self, text: str) -> Tuple[bool, float]:
        """Predict if text is spam. Returns (is_spam, probability)."""
        if not self.is_loaded or not self.pipeline:
            print("⚠️ Model not loaded. Returning default 'not spam'.")
            return False, 0.0

        processed_text = self.preprocess_text(text)
        try:
            prediction = self.pipeline.predict([processed_text])[0]
            probability = self.pipeline.predict_proba([processed_text])[0][1]
            return prediction == 1, probability
        except Exception as e:
            print(f"⚠️ Prediction failed: {e}")
            return False, 0.0

    def check_offensive(self, text: str, language: str = "en") -> bool:
        """Check if text contains offensive words for the given language."""
        text_lower = text.lower()
        offensive_list = OFFENSIVE_WORDS.get(language, OFFENSIVE_WORDS["en"])
        return any(word in text_lower for word in offensive_list)


# Global detector instance
detector = SpamDetector()

if __name__ == "__main__":
    # Quick test
    examples = [
        "You are a damn fool!",
        "Hello, how are you?",
        "Free tickets!!!"
    ]
    for msg in examples:
        spam, prob = detector.predict_spam(msg)
        offensive = detector.check_offensive(msg, "en")
        print(f"Message: {msg}\nSpam: {spam}, Probability: {prob:.2f}, Offensive: {offensive}\n")
