from app.database import Base, engine
from app.models import User  # Import models to ensure they're registered

Base.metadata.create_all(bind=engine)
print("Tables created successfully!")