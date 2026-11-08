# LearnMate Classroom (children's edition)

A friendly AI teacher for primary (P1-P7) and secondary (S1-S6) learners in Uganda.
Built with Streamlit, Groq (Llama 3.3 70B + Whisper), Chroma and gTTS.

## Run on Replit
1. Install packages (Shell): `pip install -r requirements.txt`
2. Add a Secret named `GROQ_API_KEY` (and optionally `TEACHER_PIN`).
3. Run command: `streamlit run app.py --server.port 5000 --server.address 0.0.0.0`

## Folders
- `materials/`   learning notes, one subfolder per class (see `materials/README.txt`)
- `curriculums/` the syllabus per class and subject; the teacher stays inside it (see `curriculums/README.txt`)
- `chroma_db/`   search index, created automatically. Do not upload it; it is git-ignored.

If you are moving from the old medical version, delete the old `textbooks/` and `chroma_db/`
folders. The app also clears an old index by itself when the materials change.

## Settings
- `LIBRARY_LINKS` at the top of `app.py` controls the website buttons in the sidebar.
- `TEACHER_PIN` (secret): when set, a teacher/parent panel appears for session-only uploads.
  When it is not set, learners cannot upload anything.
