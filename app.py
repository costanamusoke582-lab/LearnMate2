import streamlit as st
import requests
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.documents import Document
from langchain_groq import ChatGroq as _GroqLLM
from groq import Groq as GroqClient
import os
import json
import tempfile
from gtts import gTTS
import io
import urllib.parse
from streamlit_mic_recorder import mic_recorder
from pptx import Presentation
import re
import streamlit.components.v1 as components
import base64

# PDF Generation Imports (Fixes SimpleDocTemplate NameError)
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader

# --- DEFAULT CONFIGURATION ---
# Paste your Groq API key here to avoid having to type it in every single session:
DEFAULT_API_KEY = ""

# --- DIRECTORIES ---
os.makedirs("textbooks", exist_ok=True)
os.makedirs("curriculums", exist_ok=True)
CHROMA_DIR = "./chroma_db"
INDEX_FILE = "./chroma_db/indexed_files.json"
google_drive_link = "https://drive.google.com/drive/folders/1FQfoI42BOoOsUHWbhYk2BwygHig0ur-R?usp=sharing"

st.set_page_config(
    page_title="LearnMate Classroom", layout="wide", initial_sidebar_state="expanded"
)

# Elegant, highly legible premium light-mode styling to fix the dark UI issue
st.markdown(
    """
    <style>
    html, body, [class*="css"] { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif !important; }
    .main .block-container { padding: 1.5rem 2rem 3rem 2rem; max-width: 1280px; background-color: #f8fafc; }
    [data-testid="stSidebar"] {
        background: linear-gradient(160deg, #f1f5f9 0%, #e2e8f0 100%) !important;
        border-right: 1px solid #cbd5e1;
    }
    [data-testid="stSidebar"] * { color: #1e293b !important; }
    [data-testid="stSidebar"] .stSelectbox label,
    [data-testid="stSidebar"] .stTextInput label { color: #475569 !important; font-size: 0.75rem !important; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
    [data-testid="stSidebar"] [data-baseweb="select"] > div,
    [data-testid="stSidebar"] [data-baseweb="input"] > div { background: #ffffff !important; border: 1px solid #cbd5e1 !important; border-radius: 8px !important; color: #1e293b !important; }
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3 { color: #0f172a !important; font-size: 0.85rem !important; font-weight: 700 !important; text-transform: uppercase; letter-spacing: 0.08em; }
    [data-testid="stSidebar"] hr { border-color: #cbd5e1 !important; margin: 0.75rem 0 !important; }
    [data-testid="stSidebar"] .stButton > button {
        width: 100%; background: #4f46e5 !important;
        border: 1px solid #4338ca !important; border-radius: 8px !important;
        color: #ffffff !important; font-size: 0.82rem !important;
        font-weight: 600; transition: all 0.2s;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: #4338ca !important; color: #fff !important;
        transform: translateY(-1px); box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
    }
    h1 { font-size: 2.25rem !important; font-weight: 800 !important; color: #0f172a !important; letter-spacing: -0.02em; }
    [data-testid="stChatMessage"] {
        border-radius: 14px !important; padding: 1rem 1.25rem !important;
        margin-bottom: 0.75rem !important; border: 1px solid #e2e8f0 !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
        background: #ffffff !important;
        color: #334155 !important;
    }
    [data-testid="stChatMessage"][data-testid*="user"] { background: #f0fdf4 !important; border-color: #bbf7d0 !important; color: #14532d !important; }
    [data-testid="stChatInput"] textarea {
        border-radius: 12px !important; border: 1.5px solid #cbd5e1 !important;
        background: #ffffff !important; font-size: 0.95rem !important;
        color: #0f172a !important;
    }
    [data-testid="stChatInput"] textarea:focus { border-color: #4f46e5 !important; }
    [data-testid="stExpander"] { border-radius: 10px !important; border: 1px solid #e2e8f0 !important; overflow: hidden; background: #ffffff !important;}
    div[data-testid="stExpander"] div[role="region"] { 
        max-height: 300px !important; 
        overflow-y: auto !important; 
        padding-right: 10px !important; 
    }
    div[data-testid="stExpander"] div[role="region"]::-webkit-scrollbar { width: 5px !important; }
    div[data-testid="stExpander"] div[role="region"]::-webkit-scrollbar-thumb { background: #cbd5e1 !important; border-radius: 3px !important; }
    #MainMenu, footer { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- PDF GENERATION HELPERS ---
def generate_pdf(content, filename):
    """Standard PDF generator for transcripts (Text only)"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph("KMT Dynamics - LearnMate", styles["Title"]))
    story.append(Spacer(1, 12))

    content = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", content)
    content = re.sub(r"#+\s*(.*)", r"<b>\1</b>", content)

    for line in content.split("\n"):
        if line.strip():
            story.append(Paragraph(line.replace("\n", "<br/>"), styles["Normal"]))
            story.append(Spacer(1, 6))
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def generate_pdf_from_messages(messages, course_name):
    """Advanced PDF generator to parse chat history and fetch images"""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    title = (
        f"Lecture Discussion: {course_name}" if course_name else "Lecture Discussion"
    )
    story.append(Paragraph(title, styles["Title"]))
    story.append(Spacer(1, 12))

    for msg in messages:
        role = "Professor" if msg["role"] == "assistant" else "Student"
        story.append(Paragraph(f"<b>{role}:</b>", styles["Heading3"]))

        text = msg["content"]
        text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"#+\s*(.*)", r"<b>\1</b>", text)

        for line in text.split("\n"):
            if line.strip():
                story.append(Paragraph(line.replace("\n", "<br/>"), styles["Normal"]))
                story.append(Spacer(1, 4))

        if msg.get("image_url"):
            try:
                url = msg["image_url"]
                if url.startswith("//"):
                    url = "https:" + url

                headers = {"User-Agent": "LearnMateApp/1.0 (Contact: your@email.com)"}
                img_response = requests.get(url, headers=headers, timeout=10)

                if img_response.status_code == 200:
                    img_data = io.BytesIO(img_response.content)

                    img_reader = ImageReader(img_data)
                    iw, ih = img_reader.getSize()

                    max_width = 400
                    max_height = 380
                    scale = min(max_width / float(iw), max_height / float(ih), 1.0)
                    new_width = iw * scale
                    new_height = ih * scale

                    img_data.seek(0)
                    img = RLImage(img_data, width=new_width, height=new_height)
                    img.hAlign = "CENTER"

                    story.append(Spacer(1, 6))
                    story.append(img)

                    if msg.get("image_caption"):
                        story.append(
                            Paragraph(
                                f"<font size=8><i>Fig: {msg['image_caption']}</i></font>",
                                styles["Normal"],
                            )
                        )
                else:
                    story.append(
                        Paragraph(
                            f"<i>[Image download failed. Server returned status code: {img_response.status_code}]</i>",
                            styles["Normal"],
                        )
                    )
            except Exception as e:
                story.append(
                    Paragraph(
                        f"<i>[Image failed to load in PDF: {str(e)}]</i>",
                        styles["Normal"],
                    )
                )

        story.append(Spacer(1, 12))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


# --- GROQ LLM CLIENT (drop-in wrapper; keeps .invoke() interface used throughout app) ---
class ChatGoogleGenerativeAI:
    """Groq-backed LLM. Keeps this class name to avoid changing every call site."""
    def __init__(self, model, google_api_key):
        self._llm = _GroqLLM(model="llama-3.3-70b-versatile", api_key=google_api_key)

    def invoke(self, input_data):
        if isinstance(input_data, str):
            result = self._llm.invoke([HumanMessage(content=input_data)])
        else:
            result = self._llm.invoke(input_data)

        class ResponseMock:
            def __init__(self, content):
                self.content = content

        return ResponseMock(result.content)


# --- VOICE & PPTX HELPERS ---
def transcribe_audio(audio_bytes, api_key):
    """Transcribe audio using Groq Whisper."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        client = GroqClient(api_key=api_key)
        with open(tmp_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=f,
                response_format="text",
            )
        return transcription if isinstance(transcription, str) else transcription.text
    except Exception as e:
        return f"Transcription error: {str(e)}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load_pptx_as_docs(path, filename):
    prs = Presentation(path)
    text_runs = [
        "\n".join(
            shape.text.strip()
            for shape in slide.shapes
            if hasattr(shape, "text") and shape.text.strip()
        )
        for slide in prs.slides
    ]
    full = "\n\n".join([t for t in text_runs if t])
    return [Document(page_content=full, metadata={"source": filename})] if full else []


# --- FAST PERSISTENT LOADER ---
def get_textbook_filelist():
    base = "textbooks"
    return sorted(
        [
            fn
            for fn in os.listdir(base)
            if os.path.isfile(os.path.join(base, fn))
            and os.path.getsize(os.path.join(base, fn)) > 0
            and os.path.splitext(fn)[1].lower()
            in (".pdf", ".pptx", ".jpg", ".jpeg", ".png")
        ]
    )


def get_indexed_filelist():
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r") as f:
            return json.load(f)
    return []


def save_indexed_filelist(files):
    os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
    with open(INDEX_FILE, "w") as f:
        json.dump(files, f)


@st.cache_resource(show_spinner=False)
def load_textbooks():
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    current_files = get_textbook_filelist()
    indexed = get_indexed_filelist()

    if os.path.exists(CHROMA_DIR) and current_files == indexed and current_files:
        return Chroma(
            persist_directory=CHROMA_DIR, embedding_function=embeddings
        ), current_files

    docs = []
    for fn in current_files:
        path = os.path.join("textbooks", fn)
        ext = os.path.splitext(fn)[1].lower()
        try:
            if ext == ".pdf":
                loaded = PyPDFLoader(path).load()
            elif ext == ".pptx":
                loaded = load_pptx_as_docs(path, fn)
            else:
                loaded = [
                    Document(
                        page_content=f"Image file: {fn} (visual content not extracted yet)",
                        metadata={"source": fn},
                    )
                ]
            for d in loaded:
                d.metadata["source"] = fn
            docs.extend(loaded)
        except:
            pass

    if docs:
        chunks = RecursiveCharacterTextSplitter(
            chunk_size=1000, chunk_overlap=200
        ).split_documents(docs)
        vectorstore = Chroma.from_documents(
            chunks, embeddings, persist_directory=CHROMA_DIR
        )
        save_indexed_filelist(current_files)
        return vectorstore, current_files
    return None, []


@st.cache_data(show_spinner=False)
def load_curriculums():
    curr_dict = {}
    curr_folder = "curriculums"
    if os.path.exists(curr_folder):
        for file in os.listdir(curr_folder):
            file_path = os.path.join(curr_folder, file)
            if os.path.isfile(file_path):
                course_name = os.path.splitext(file)[0].strip().lower()
                with open(file_path, "r", encoding="utf-8") as f:
                    curr_dict[course_name] = f.read().strip()
    return curr_dict


curriculum_db = load_curriculums()
_vs, _sources = load_textbooks()

# --- SESSION STATE ---
states = {
    "messages": [],
    "lecture_outline": None,
    "current_test": None,
    "test_feedback": None,
    "test_history": [],
    "last_response": "",
    "autoplay_audio": False,
    "last_processed_audio_id": None,
    "sources_list": _sources,
    "vectorstore": _vs,
    "kb_loaded": bool(_vs),
    "tts_voice": "troy",
}
for key, val in states.items():
    if key not in st.session_state:
        st.session_state[key] = val

st.session_state.vectorstore = _vs
st.session_state.sources_list = _sources
st.session_state.kb_loaded = bool(_vs)

st.title("🎓 LearnMate Classroom")

# --- SIDEBAR ---
with st.sidebar:
    st.header("⚙️ Settings")
    api_key = os.getenv("GROQ_API_KEY") or DEFAULT_API_KEY
    selected_model = "llama-3.3-70b-versatile"

    level = st.selectbox("Level", ["Beginner", "Intermediate", "Advanced"], index=1)
    year = st.selectbox(
        "Year", ["Year 1", "Year 2", "Year 3", "Year 4", "Year 5", "Year 6"], index=0
    )
    semester = st.selectbox("Semester", ["Semester 1", "Semester 2"], index=0)
    course = st.text_input("Course", value="Medical Sciences")
    topic = st.text_input("Focus Topic", value="Anatomy")

    st.divider()
    st.header("🎙️ Voice")
    audio_input = mic_recorder(
        start_prompt="Speak to Professor", stop_prompt="Process Voice", key="mic"
    )

    voice_options = {
        "Troy (Male, Professional)": "troy",
        "Diana (Female, Clear)": "diana",
        "Hannah (Female, Warm)": "hannah",
        "Austin (Male, Deep)": "austin",
        "Autumn (Female, Soft)": "daniel",
        "Daniel (Male, Natural)": "daniel",
    }
    chosen_voice_label = st.selectbox(
        "Professor Voice", list(voice_options.keys()), key="voice_label"
    )
    st.session_state.tts_voice = voice_options[chosen_voice_label]

    st.divider()
    st.header("📚 Knowledge Base (Temporary Upload)")

    temp_uploaded = st.file_uploader(
        "Upload files for this session only",
        type=["pdf", "pptx", "jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key="temp_uploader",
    )

    if temp_uploaded and st.button("🔄 Process Files Temporarily"):
        with st.spinner("Adding files to this session only..."):
            temp_docs = []
            for file in temp_uploaded:
                bytes_data = file.getvalue()
                ext = os.path.splitext(file.name)[1].lower()
                try:
                    if ext == ".pdf":
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=".pdf"
                        ) as tmp:
                            tmp.write(bytes_data)
                            loaded = PyPDFLoader(tmp.name).load()
                        os.unlink(tmp.name)
                    elif ext == ".pptx":
                        with tempfile.NamedTemporaryFile(
                            delete=False, suffix=".pptx"
                        ) as tmp:
                            tmp.write(bytes_data)
                            loaded = load_pptx_as_docs(tmp.name, file.name)
                        os.unlink(tmp.name)
                    else:
                        loaded = [
                            Document(
                                page_content=f"Image uploaded: {file.name} (visual content not extracted yet)",
                                metadata={"source": file.name, "temporary": True},
                            )
                        ]
                    for d in loaded:
                        d.metadata["source"] = file.name
                        d.metadata["temporary"] = True
                    temp_docs.extend(loaded)
                except Exception as e:
                    st.warning(f"Could not process {file.name}")

            if temp_docs:
                chunks = RecursiveCharacterTextSplitter(
                    chunk_size=1000, chunk_overlap=200
                ).split_documents(temp_docs)
                if st.session_state.vectorstore is None:
                    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
                    st.session_state.vectorstore = Chroma.from_documents(
                        chunks, embeddings
                    )
                else:
                    st.session_state.vectorstore.add_documents(chunks)

                st.session_state.sources_list.extend(
                    [d.metadata["source"] for d in temp_docs]
                )
                st.session_state.kb_loaded = True
                st.success("✅ Files added for this session only!")
                st.rerun()

    if st.session_state.kb_loaded:
        st.success(f"✅ {len(st.session_state.sources_list)} files in session")
    else:
        st.warning("No files in current session. Upload above to use temporarily.")

    st.divider()
    st.header("📚 Digital Library")
    st.link_button("☁️ Open Google Drive Library", google_drive_link)


# --- DASHBOARD NAVIGATION TABS ---
classroom_tabs = st.tabs(
    [
        "💬 Lecture Chat",
        "📝 Exam Room & Transcript",
    ]
)

# ==================== TAB 1: LECTURE HALL ====================
with classroom_tabs[0]:
    st.subheader("💬 Active Class Discussion")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and "image_url" in msg:
                st.image(msg["image_url"], caption=msg.get("image_caption", ""))

    if st.session_state.messages:
        pdf_bytes = generate_pdf_from_messages(st.session_state.messages, course)
        st.download_button(
            "📥 Download Full Lecture Discussion (PDF)",
            pdf_bytes,
            "LearnMate_Lecture_Discussion.pdf",
            "application/pdf",
        )

    prompt = st.chat_input("Ask your professor...")
    if audio_input and audio_input.get("id") != st.session_state.get(
        "last_processed_audio_id"
    ):
        with st.spinner("Professor is listening..."):
            try:
                prompt = transcribe_audio(audio_input["bytes"], api_key)
                st.session_state.last_processed_audio_id = audio_input.get("id")
            except Exception as e:
                st.error(f"Error transcribing audio: {e}")

    if prompt:
        if not api_key:
            st.error("Please enter your Groq API Key.")
            st.stop()
        if not st.session_state.vectorstore:
            st.error("Knowledge Base not loaded yet.")
            st.stop()

        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            llm = ChatGoogleGenerativeAI(model=selected_model, google_api_key=api_key)
            full_response = ""
            generated_image_url = None
            generated_image_caption = None

            try:
                if (
                    key_len := len(st.session_state.messages) == 1
                    and not st.session_state.lecture_outline
                ):
                    st.session_state.lecture_outline = llm.invoke(
                        f"Give a clear structured outline for teaching {topic} in {course} at {level} level."
                    ).content
                    full_response += f"### 📋 Topic Outline\n{st.session_state.lecture_outline}\n\n---\n\n"

                context = ""
                references = []
                if st.session_state.vectorstore:
                    docs = st.session_state.vectorstore.as_retriever(
                        search_kwargs={"k": 4}
                    ).invoke(prompt)
                    context = "\n\n".join(
                        [
                            f"Source {d.metadata.get('source')}: {d.page_content}"
                            for d in docs
                        ]
                    )
                    references = [
                        f"📘 {d.metadata.get('source')}: {d.page_content[:150]}..."
                        for d in docs
                    ]

                curr = curriculum_db.get(course.lower().strip(), "No curriculum found.")

                sys_msg = f"""You are a Professor teaching {course} to a {year} student in {semester}.
Current Focus Topic: {topic}
Curriculum: {curr}
RULES:
- You are currently lecturing specifically on: {topic}. Stay focused on this topic unless the student explicitly asks about something else.
- Use the provided textbook, pdf and pptx content found in textbook folder as your primary source.
- Reference it by name where relevant.
- If a topic is not covered, use general knowledge.
- Be detailed try to exhaust everything, professional, and follow the lecture outline.
- Never mention that you are using general knowledge or that textbooks are unavailable.
- You dont have to show an image if you dont have to.

VISUAL ILLUSTRATIONS (CRITICAL INSTRUCTIONS):
Whenever explaining a complex physical, scientific, biological, or abstract concept, you MUST request an illustration from the database. 
To do this, you must output EXACTLY this format with a SHORT search query (not a long description):
[GENERATE_IMAGE: "short search keyword"]

Example for a heart: [GENERATE_IMAGE: "human heart diagram"]
Example for a cell: [GENERATE_IMAGE: "animal cell"]

Textbook Context:
{context}"""

                history = [HumanMessage(content=sys_msg)] + [
                    HumanMessage(content=m["content"])
                    if m["role"] == "user"
                    else AIMessage(content=m["content"])
                    for m in st.session_state.messages[-5:]
                ]
                raw_answer = llm.invoke(history).content

                if "[GENERATE_IMAGE:" in raw_answer:
                    try:
                        start_idx = raw_answer.find('[GENERATE_IMAGE: "') + 18
                        end_idx = raw_answer.find('"]', start_idx)
                        search_query = raw_answer[start_idx:end_idx]
                        generated_image_caption = search_query

                        clean_answer = re.sub(
                            r'\[GENERATE_IMAGE:\s*"[^"]*"\]', "", raw_answer
                        ).strip()
                        full_response += clean_answer
                        st.session_state.last_response = clean_answer
                        st.markdown(clean_answer)

                        with st.spinner(f"🔍 Searching library for: {search_query}..."):
                            wiki_url = "https://en.wikipedia.org/w/api.php"
                            headers = {
                                "User-Agent": "LearnMateApp/1.0 (educational-tool)"
                            }

                            # Try original full-resolution image first
                            params = {
                                "action": "query",
                                "format": "json",
                                "prop": "pageimages",
                                "generator": "search",
                                "gsrsearch": search_query,
                                "gsrlimit": 5,
                                "piprop": "original",
                            }
                            response = requests.get(wiki_url, params=params, headers=headers, timeout=8)
                            data = response.json()
                            pages = data.get("query", {}).get("pages", {})

                            # Pick the first page that has an original image
                            for page_id, page_info in sorted(pages.items(), key=lambda x: x[1].get("index", 999)):
                                orig = page_info.get("original", {})
                                url = orig.get("source", "")
                                # Skip SVG and small images
                                if url and not url.lower().endswith(".svg"):
                                    w = orig.get("width", 0)
                                    h = orig.get("height", 0)
                                    if w >= 300 and h >= 200:
                                        generated_image_url = url
                                        break

                            # Fallback: high-res thumbnail
                            if not generated_image_url:
                                params["piprop"] = "thumbnail"
                                params["pithumbsize"] = 1200
                                response = requests.get(wiki_url, params=params, headers=headers, timeout=8)
                                data = response.json()
                                pages = data.get("query", {}).get("pages", {})
                                for page_id, page_info in sorted(pages.items(), key=lambda x: x[1].get("index", 999)):
                                    url = page_info.get("thumbnail", {}).get("source", "")
                                    if url and not url.lower().endswith(".svg"):
                                        generated_image_url = url
                                        break

                            if generated_image_url:
                                st.image(
                                    generated_image_url,
                                    caption=f"📖 {search_query.title()} — via Wikipedia",
                                    use_container_width=True,
                                )
                            else:
                                st.info(
                                    "No suitable diagram found in the database for this concept."
                                )

                    except Exception as e:
                        st.markdown(raw_answer)
                        st.session_state.last_response = raw_answer
                        full_response += raw_answer
                        st.error(f"Image fetching error: {e}")
                else:
                    full_response += raw_answer
                    st.session_state.last_response = raw_answer
                    st.markdown(raw_answer)

                if references:
                    with st.expander("📚 References"):
                        for ref in references:
                            st.write(ref)

                history_entry = {"role": "assistant", "content": full_response}
                if generated_image_url:
                    history_entry["image_url"] = generated_image_url
                    history_entry["image_caption"] = generated_image_caption

                st.session_state.messages.append(history_entry)

                st.rerun()

            except Exception as e:
                if "429" in str(e) or "Rate limit" in str(e):
                    st.error(
                        "⚠️ Groq API Rate Limit Reached! Please wait a moment and try asking your question again."
                    )
                else:
                    st.error(f"An unexpected error occurred: {e}")
                st.stop()

    if st.session_state.get("last_response"):
        if st.button("🔊 Hear Professor's Response", use_container_width=True):
            with st.spinner("🔊 Generating voice..."):
                try:
                    text_to_read = st.session_state.last_response
                    if len(text_to_read) > 1000:
                        cut_index = text_to_read.rfind(".", 0, 1000)
                        if cut_index == -1:
                            cut_index = 1000
                        tts_text = (
                            text_to_read[: cut_index + 1]
                            + " You can read the rest of my detailed explanation on the screen."
                        )
                    else:
                        tts_text = text_to_read

                    tts = gTTS(text=tts_text, lang="en")
                    fp = io.BytesIO()
                    tts.write_to_fp(fp)
                    fp.seek(0)
                    audio_bytes = fp.read()

                    st.audio(audio_bytes, format="audio/mpeg", autoplay=True)

                except Exception as e:
                    st.error(f"Error playing audio: {e}")


# ==================== TAB 2: EXAM ROOM ====================
@st.dialog("📝 Practice Exam Room", width="large")
def exam_dialog():
    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("### ⚙️ Exam Settings")
        q_num = st.slider("Number of questions", 3, 20, 10, key="exam_dlg_questions")
        if st.button("🚀 Generate Exam", use_container_width=True):
            if not api_key:
                st.error("Groq API Key required.")
                return
            llm = ChatGoogleGenerativeAI(model=selected_model, google_api_key=api_key)
            all_topics = [h.get("topic", "") for h in st.session_state.test_history] + [
                topic or "General"
            ]
            unique_topics = list(dict.fromkeys([t for t in all_topics if t]))
            test_prompt = (
                f"Generate a fresh {q_num}-question exam covering: {', '.join(unique_topics)}.\n"
                "Make questions different from previous tests.\n"
                "Format: Numbered questions with 4 clear MCQ options (A-D). Do NOT give answers."
            )
            try:
                st.session_state.current_test = llm.invoke(test_prompt).content
                st.session_state.test_feedback = None
                st.rerun()
            except Exception as e:
                st.error(f"Error generating exam: {e}")

        if st.session_state.current_test:
            st.divider()
            st.markdown("### ✏️ Your Answers")
            student_ans = st.text_area(
                "Enter answers (e.g. 1.A, 2.C ...):",
                height=260,
                key="exam_dlg_ans",
            )
            if st.button("📝 Submit for Grading", use_container_width=True):
                if not api_key:
                    st.error("Groq API Key required.")
                    return
                llm = ChatGoogleGenerativeAI(model=selected_model, google_api_key=api_key)
                grading_prompt = f"""Grade this student's work professionally and provide corrections.
Topic: {topic or "General"}
Questions: {st.session_state.current_test}
Student Answers: {student_ans}
Provide:
- Percentage score (0-100)
- Letter grade (A-F)
- Detailed feedback with corrections (explain right answers where wrong, strengths, weaknesses)
"""
                try:
                    feedback = llm.invoke(grading_prompt).content
                    st.session_state.test_history.append(
                        {
                            "topic": topic or "General",
                            "test": st.session_state.current_test,
                            "answer": student_ans,
                            "feedback": feedback,
                        }
                    )
                    st.session_state.test_feedback = feedback
                    st.rerun()
                except Exception as e:
                    st.error(f"Error grading exam: {e}")

            if st.button("🗑️ Clear & Start New Exam", use_container_width=True):
                st.session_state.current_test = None
                st.session_state.test_feedback = None
                st.rerun()

    with col2:
        if st.session_state.test_feedback:
            st.markdown("### 🎓 Assessment Results")
            st.markdown(st.session_state.test_feedback)
        elif st.session_state.current_test:
            st.markdown("### 📋 Exam Questions")
            st.markdown(st.session_state.current_test)
        else:
            st.info("Click **Generate Exam** on the left to begin your practice session.")


with classroom_tabs[1]:
    st.subheader("📝 Exam Room & Transcripts")
    st.write("Launch a full-screen exam session or review your past results below.")

    if st.button("🚀 Open Exam Room", use_container_width=True, type="primary"):
        exam_dialog()

    st.divider()
    st.markdown("### 📜 Session Transcripts")

    if st.session_state.test_history:
        report_text = f"ACADEMIC TRANSCRIPT: {course or 'General'}\n" + "=" * 40 + "\n"
        for h in st.session_state.test_history:
            report_text += (
                f"\nTopic: {h['topic']}\nFeedback:\n{h['feedback']}\n{'-' * 40}\n"
            )
        pdf_bytes = generate_pdf(report_text, "transcript.pdf")
        st.download_button(
            "📥 Download Academic Transcript (PDF)",
            pdf_bytes,
            "academic_transcript.pdf",
            "application/pdf",
        )
        if st.button("🗑️ Clear Transcript Database"):
            st.session_state.test_history = []
            st.rerun()
    else:
        st.write("Complete an exam session to populate transcript options.")
