import uvicorn

from app.config import Settings


def main():
    uvicorn.run("app.api:app", host="0.0.0.0", port=Settings.from_env().api_port)


if __name__ == "__main__":
    main()
