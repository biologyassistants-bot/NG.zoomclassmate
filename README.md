# ClassMate 🎓

A study assistant web app for your students. They pick a class recording, ask questions, and get answers grounded **only** in that recording's transcript — with timestamp citations. It also generates quizzes, supports multiple languages, and is locked down to your class roster.

This guide gets you from this folder to a **permanent public link** you can share with students, using a **free** cloud host.

---

## What's in this folder

```
classmate/
├── backend/            FastAPI server + your recordings data
│   ├── main.py
│   ├── requirements.txt
│   └── data/
│       ├── recordings.json     ← your 40 Zoom recordings + transcripts
│       ├── roster.json         ← your class roster (starts empty)
│       ├── config.json         ← teacher passcode
│       └── question_log.json   ← log of student questions
├── frontend/           The student + teacher web interface
├── Dockerfile          For Docker-based hosts (Railway, Fly.io, etc.)
├── render.yaml         One-click config for Render
├── Procfile            For Railway / Heroku-style hosts
├── .env.example        Environment variables template
└── README.md           This file
```

---

## Before you deploy: get an AI key (2 minutes)

In this preview the answers were powered by the built-in sandbox AI. On your own host you supply your own AI key — the app uses any **OpenAI-compatible** API.

1. Go to https://platform.openai.com/api-keys and create a key (starts with `sk-`).
2. Add a few dollars of credit. The default model `gpt-4o-mini` is very cheap — a whole class asking questions typically costs cents per day.

You'll paste this key into the host's dashboard in the steps below (never commit it to code).

---

## Option A — Deploy on Render (easiest, free)

1. **Put the code on GitHub.**
   - Create a free GitHub account if needed, make a new repository (e.g. `classmate`), and upload this folder's contents (drag-and-drop works on github.com → "Add file" → "Upload files").

2. **Create the service on Render.**
   - Sign up at https://render.com (free).
   - Click **New → Web Service**, connect your GitHub, and pick the `classmate` repo.
   - Render reads `render.yaml` automatically. If asked, confirm: environment **Python**, root directory **backend**, start command `uvicorn main:app --host 0.0.0.0 --port $PORT`.

3. **Add your AI key.**
   - In the service's **Environment** tab, add a variable:
     - `OPENAI_API_KEY` = your `sk-...` key
   - (Optional) `OPENAI_MODEL` = `gpt-4o-mini`

4. **Deploy.** Render builds and gives you a link like:
   `https://classmate-xxxx.onrender.com`

   **That's your student link.** 🎉 It uses HTTPS automatically.

> Note: Render's free tier "sleeps" after inactivity, so the first visit after a quiet period takes ~30 seconds to wake. Fine for a class; upgrade to a paid plan (~$7/mo) to keep it always-on.

---

## Option B — Deploy on Railway (also free-ish, no sleep)

1. Put the code on GitHub (same as above).
2. Sign up at https://railway.app, click **New Project → Deploy from GitHub repo**, pick your repo.
3. Railway detects the `Dockerfile` and builds it.
4. In **Variables**, add `OPENAI_API_KEY` = your key.
5. Under **Settings → Networking**, click **Generate Domain** → you get your public student link.

---

## Option C — Any Docker host (Fly.io, a VPS, etc.)

The included `Dockerfile` runs the whole app. Example on your own machine or server:

```bash
docker build -t classmate .
docker run -p 8100:8100 -e OPENAI_API_KEY=sk-your-key classmate
```

Then put it behind HTTPS (Fly.io does this for you; on a VPS use Caddy or Nginx).

---

## First things to do once it's live

1. Open your link, choose **👩‍🏫 I'm the teacher**, passcode `teach123`.
2. Go to **Settings** and change the passcode immediately.
3. Go to **Students**, add each student — the app generates a **4-digit PIN** per student. Share each student their name + PIN privately.
4. In **Recordings**, rename recordings and assign them to units (e.g. "Unit 1"); hide any you don't want students to see.
5. Students open the same link, choose **🎒 I'm a student**, and sign in with their name + PIN.

---

## Security notes (please read)

- **Roster gating:** only students you add can log in; every question/quiz call requires a valid login session.
- **Change the default passcode** (`teach123`) before sharing the link.
- **HTTPS:** Render/Railway/Fly give you HTTPS automatically — this encrypts PINs in transit. If self-hosting, put the app behind HTTPS.
- **PINs are 4 digits** for convenience. For stronger security you can change students' PINs anytime ("New PIN" button), or ask your developer to switch to longer codes / Google sign-in.
- Your recordings and transcripts live in `backend/data/` and are only served to logged-in students.

---

## Updating recordings later

Re-run the recording export (from the environment where you first built this) to refresh `backend/data/recordings.json`, commit the change to GitHub, and your host redeploys automatically. Your roster and question log are separate files and won't be overwritten.
