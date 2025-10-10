
import os
import pickle
import nltk
from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.naive_bayes import MultinomialNB
from sklearn.metrics import classification_report

# Ensure punkt tokenizer is available
nltk.download("punkt", quiet=True)
nltk.download("punkt_tab", quiet=True)


# Sample training dataset
texts = [
    "You have won free airtime!",  # spam
    "Claim your free prize now",   # spam
    "Congratulations! You won 1 million",  # spam
    "Report road blockage in Lira town",   # not spam
    "How can we improve healthcare?",      # not spam
    "Please raise civic issues with your MP",  # not spam
    "Earn money fast by clicking this link",  # spam
    "Meeting at district office tomorrow",  # not spam
]

labels = [1, 1, 1, 0, 0, 0, 1, 0]  # 1 = spam, 0 = not spam


# Train TF-IDF + Naive Bayes
vectorizer = TfidfVectorizer(stop_words="english")
X = vectorizer.fit_transform(texts)
y = labels

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42)
model = MultinomialNB()
model.fit(X_train, y_train)


# Evaluate model
y_pred = model.predict(X_test)
print("\n Classification Report:\n")
print(classification_report(y_test, y_pred))


# Save model & vectorizer
model_path = os.path.join(os.getcwd(), "spam_model.pkl")
vectorizer_path = os.path.join(os.getcwd(), "tfidf_vectorizer.pkl")

with open(model_path, "wb") as f:
    pickle.dump(model, f)
with open(vectorizer_path, "wb") as f:
    pickle.dump(vectorizer, f)

print(f"\n✅ Model saved to {model_path}")
print(f"✅ Vectorizer saved to {vectorizer_path}")
