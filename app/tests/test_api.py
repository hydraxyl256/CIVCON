import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport
from app.main import app

# ------------------------
# Fixtures
# ------------------------

@pytest_asyncio.fixture(scope="module")
async def client():
    """Provides an AsyncClient attached to the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

@pytest_asyncio.fixture(scope="module")
async def test_user_email():
    """Generate a unique email for testing."""
    return f"pytest_{uuid.uuid4().hex}@example.com"

@pytest_asyncio.fixture(scope="module")
async def token(client, test_user_email):
    """Create a test user and return a valid access token."""
    
    # Signup
    signup_res = await client.post("/auth/signup", json={
        "first_name": "Test",
        "last_name": "User",
        "email": test_user_email,
        "password": "secret123",
        "confirm_password": "secret123"
    })
    assert signup_res.status_code in [200, 201], f"Signup failed: {signup_res.text}"

    # Login using OAuth2PasswordRequestForm expects 'username' and 'password' as form data
    login_res = await client.post("/auth/login", data={
        "username": test_user_email,
        "password": "secret123"
    })
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    return login_res.json()["access_token"]

# ------------------------
# Tests
# ------------------------

@pytest.mark.asyncio
async def test_login(client, test_user_email):
    """Test logging in with the test user."""
    res = await client.post("/auth/login", data={
        "username": test_user_email,
        "password": "secret123"
    })
    assert res.status_code == 200
    assert "access_token" in res.json()

@pytest.mark.asyncio
async def test_get_users(client, token):
    """Test fetching all users with auth token."""
    res = await client.get(
        "/users/",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200
    assert isinstance(res.json(), list)

@pytest.mark.asyncio
async def test_get_user_by_id(client, token):
    """Test fetching a single user by ID with auth token."""
    res = await client.get(
        "/users/1",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code in [200, 404]  # 404 if user with ID 1 does not exist
