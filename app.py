from flask import Flask, render_template

app = Flask(__name__)

@app.route("/")
def home():
    return 'hello s2'

if __name__ == "__main__":
    app.run()
