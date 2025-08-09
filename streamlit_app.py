import streamlit as st
import openai, re, os, shutil, io, time, uuid, zipfile, jwt, requests, coolname, base64

st.set_page_config(page_title="volt", page_icon="⚡")
openai.api_key=st.secrets['OPENAI_API_KEY']
pat = st.secrets['NETLIFY_PAT']
team_slug = st.secrets['NETLIFY_TEAM_SLUG']
API_BASE = "https://api.netlify.com/api/v1"

# Import system prompt from file
with open('system_prompt.md', 'r', encoding='utf-8') as f:
    system_prompt = f.read()

# Function to load default HTML content
def load_default_html() -> str:
    try:
        with open('default_index.html', 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        # tiny safe fallback if file is missing
        return """<!doctype html><html><head><meta charset=\"utf-8\"><title>Welcome</title></head>\n<body><h1>Here Be Dragons 🐉</h1><p>Ask me to generate some HTML!</p></body></html>"""

# Initialize session state variables
if "chat_history" not in st.session_state:
    st.session_state.chat_history = [{"role": "system", "content": system_prompt}]
if "html_version" not in st.session_state:
    st.session_state.html_version = 0
if "app_name" not in st.session_state:
    st.session_state.app_name = '-'.join(coolname.generate())
if "html" not in st.session_state:
    st.session_state.html = load_default_html()

avatar = {'user': '⚡', 'assistant': '🤖', 'system': '🔧'}
model = 'gpt-4.1-mini'
AUTH0_DOMAIN = st.secrets["auth"]["domain"]

def zip_from_html_str(html_str: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html_str)  # in-memory file
    buf.seek(0)
    return buf.read()

def get_github_token(auth0_user_id: str) -> str | None:
    mgmt = requests.post(f"https://{AUTH0_DOMAIN}/oauth/token", json={
    "client_id": st.secrets["auth"]["client_id"],
    "client_secret": st.secrets["auth"]["client_secret"],
    "audience": f"https://{AUTH0_DOMAIN}/api/v2/",
    "grant_type": "client_credentials",
    }).json()["access_token"]
    r = requests.get(
        f"https://{AUTH0_DOMAIN}/api/v2/users/{auth0_user_id}",
        headers={"Authorization": f"Bearer {mgmt}"},
        params={"fields": "identities", "include_fields": "true"},
    )
    r.raise_for_status()
    for ident in r.json().get("identities", []):
        if ident.get("provider") == "github":
            return ident.get("access_token")
    return None


def create_new_repo(token, repo_name):
    headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
    }
    r = requests.post("https://api.github.com/user/repos",
                    json={"name":repo_name,"private":True,"auto_init":True},
                    headers=headers)
    repo = r.json()
    owner, name = repo["owner"]["login"], repo["name"]
    return owner, name

def push_to_github(token, owner, name, message="Commit from Volt ⚡", version=st.session_state.html_version):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # 1) Find default branch
    r = requests.get(f"https://api.github.com/repos/{owner}/{name}", headers=headers)
    r.raise_for_status()
    branch = r.json().get("default_branch", "main")

    # 2) Read & encode content from in-memory HTML
    html_bytes = st.session_state.html.encode("utf-8")
    content = base64.b64encode(html_bytes).decode("utf-8")

    url = f"https://api.github.com/repos/{owner}/{name}/contents/index.html"

    # 3) See if the file already exists (to get its sha)
    stat = requests.get(url, headers=headers, params={"ref": branch})

    payload = {"message": message + f" version {version}", "content": content, "branch": branch}
    if stat.status_code == 200:
        payload["sha"] = stat.json()["sha"]  # required to update
    elif stat.status_code == 404:
        pass  # creating new file on this branch
    else:
        # Helpful when debugging 422 and others
        raise requests.HTTPError(f"Stat failed: {stat.status_code} {stat.text}")

    put = requests.put(url, json=payload, headers=headers)
    try:
        put.raise_for_status()
    except requests.HTTPError:
        # Print server's error details (e.g. "No commit found for the ref", "sha wasn't supplied")
        print(put.text)
        raise

def http_headers(pat, extra=None):
    h = {
        "Authorization": f"Bearer {pat}",
        "User-Agent": "netlify-e2e-script (oss)",
    }
    if extra:
        h.update(extra)
    return h

def create_site(pat, team_slug, site_name=None, session_id=None, manage_url=None):
    session_id = session_id or str(uuid.uuid4())
    payload = {
        "account_slug": team_slug,         # put site under your team
        "created_via": "Volt⚡",          # attribution in Netlify UI
        "session_id": session_id,          # must match claim JWT later
    }
    if site_name:
        payload["name"] = site_name
    if manage_url:
        payload["deploy_origin"] = {
            "name": "Volt⚡",
            "deploy_links": [
                {"url": manage_url, "name": "Manage in tool", "primary": True}
            ],
        }

    r = requests.post(f"{API_BASE}/sites",
                      json=payload,
                      headers=http_headers(pat, {"Content-Type": "application/json"}))
    if r.status_code >= 300:
        raise RuntimeError(f"Create site failed: {r.status_code} {r.text}")

    site = r.json()
    # Useful fields: id, url, admin_url
    return site, session_id

def zip_webpage() -> bytes:
    """
    Create a zip of index.html and return bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write("index.html")
    buf.seek(0)
    return buf.read()

def deploy_zip_zipmethod(pat, site_id, zip_bytes):
    """
    ZIP file deploy (official, simple): POST /sites/{site_id}/deploys
    Content-Type: application/zip, body=zip
    Returns deploy JSON with id, state, etc.
    """
    r = requests.post(
        f"{API_BASE}/sites/{site_id}/deploys",
        headers=http_headers(pat, {"Content-Type": "application/zip"}),
        data=zip_bytes,
        timeout=120,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"ZIP deploy failed: {r.status_code} {r.text}")
    return r.json()

def deploy_zip_buildapi(pat, site_id, zip_bytes, title="Initial deploy"):
    """
    Build API (multipart form): POST /sites/{site_id}/builds with fields:
      - title
      - zip (application/zip)
    """
    files = {
        "zip": ("site.zip", io.BytesIO(zip_bytes), "application/zip"),
    }
    data = {"title": title}
    r = requests.post(
        f"{API_BASE}/sites/{site_id}/builds",
        headers=http_headers(pat),
        files=files,
        data=data,
        timeout=120,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Build API deploy failed: {r.status_code} {r.text}")
    return r.json()

def poll_deploy_ready(pat, deploy_id, timeout_s=120, interval_s=3):
    """
    Poll /deploys/{deploy_id} until state == 'ready' or timeout.
    """
    deadline = time.time() + timeout_s
    last_state = None
    while time.time() < deadline:
        r = requests.get(f"{API_BASE}/deploys/{deploy_id}",
                         headers=http_headers(pat))
        if r.status_code >= 300:
            raise RuntimeError(f"Poll deploy failed: {r.status_code} {r.text}")
        state = r.json().get("state")
        if state != last_state:
            print(f"- Deploy state: {state}")
            last_state = state
        if state == "ready":
            return r.json()
        time.sleep(interval_s)
    raise TimeoutError("Timed out waiting for deploy to be ready")

def make_claim_link(oauth_client_id, oauth_client_secret, session_id, claim_webhook=None):
    """
    Create the signed JWT and produce the claim URL:
    https://app.netlify.com/claim?utm_source=volt#<jwt>

    The JWT payload requires:
      - client_id (your Netlify OAuth app client ID)
      - session_id (must match the one sent when creating the site)
    Optional:
      - claim_webhook: URL that Netlify will POST after a successful claim
    """
    payload = {
        "client_id": oauth_client_id,
        "session_id": session_id,
    }
    if claim_webhook:
        payload["claim_webhook"] = claim_webhook

    token = jwt.encode(payload, oauth_client_secret, algorithm="HS256")
    claim_url = f"https://app.netlify.com/claim?utm_source=volt#{token}"
    return claim_url

def extract_html_from_markdown(text):
    # Look for content between any type of code block markers
    patterns = [
        r'```(?:html)?\n(.*?)\n```',  # Matches ```\n or ```html\n
        r'`{3,}(.*?)`{3,}',  # Matches any number of backticks
    ]
    
    for pattern in patterns:
        matches = re.finditer(pattern, text, re.DOTALL)
        for match in matches:
            content = match.group(1).strip()
            # Check if it looks like HTML
            if content.lower().startswith(('<!doctype', '<html', '<head', '<body')):
                return content
            # Also check for any HTML-like content
            if contains_html(content):
                return content
    return None

def contains_html(text):
    # Check for common HTML patterns
    html_patterns = [
        r'<!doctype\s+html.*?>',  # DOCTYPE declaration (case insensitive)
        r'<html.*?>.*?</html>',    # Complete HTML document
        r'<body.*?>.*?</body>',    # Body tag
        r'<head.*?>.*?</head>',    # Head tag
        r'<[^>]+>',               # Any HTML tag
    ]
    return any(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in html_patterns)


def agent(chat_history, model=model):
    """Function to call the OpenAI API and handle streaming responses"""
    response = openai.chat.completions.create(
        model=model,        
        messages=chat_history,
        stream=True  # Enable streaming
    )
    report = []
    res_box = st.empty()
    
    # Iterate through the streaming response
    for chunk in response:
        if chunk.choices[0].finish_reason is None:
            # Get the delta content and append it
            report.append(chunk.choices[0].delta.content)
            # Join all pieces and strip whitespace
            result = ''.join(report).strip()
            # Update the Streamlit component with the latest text
            res_box.write(result)
    
    # Return the complete response
    return result


st.logo('img/high-voltage.png')


if not st.user.is_logged_in:
    st.title("⚡ volt")
    st.write("Welcome to **volt**! Please authenticate to start using the app.")
    st.button("Authenticate", on_click=st.login,type="primary")
    st.write("Made with ❤️ by Volt⚡")
    col1,col2,col3 = st.columns([1, 1, 1])
    with col1:
        st.markdown("[Calculator](https://calculator.vibecoders.studio/)")
        st.image('img/calculator.png', use_container_width =True)
    with col2:
        st.markdown("[Space Invaders](https://spaceinvaders.vibecoders.studio/)")
        st.image('img/spaceinvaders.png', use_container_width =True)
    with col3:
        st.markdown("[French Learning](https://french.vibecoders.studio/)")
        st.image('img/frenchlearning.png', use_container_width =True)
else:
        
    # Sidebar for chat interface
    with st.sidebar:
        prompt = st.chat_input("Enter your message here", key="chat_input")
        messages = st.container(height=450)
        # Display chat history
        for message in st.session_state.chat_history:
            if message["role"] != "system":  # Skip system messages
                with messages.chat_message(message["role"], avatar=avatar[message["role"]]):
                    st.write(message["content"])
                
        # Process user input
        if prompt:
            with messages.chat_message("user", avatar=avatar["user"]):
                st.write(prompt)
            # Append user message to chat history
            st.session_state.chat_history.append({"role": "user", "content": prompt})
            with messages.chat_message("assistant", avatar=avatar["assistant"]):
                response = agent(st.session_state.chat_history)
            # Append assistant response to chat history
            st.session_state.chat_history.append({"role": "assistant", "content": response})
            # Check for HTML content and update in-memory state if found
            html_content = extract_html_from_markdown(response)
            if html_content:
                print("Found HTML content, updating in-memory state...")  # Debug print
                st.session_state.html = html_content
                st.session_state.html_version += 1
            st.write(f"HTML Version: {st.session_state.html_version}")
        
        # Add reset button at the bottom of the sidebar
        if st.button("New App", type="primary"):
            st.session_state.chat_history = [{"role": "system", "content": system_prompt}]
            st.session_state.html_version = 0
            st.session_state.html = load_default_html()
            st.rerun()

        st.write(f"Welcome, {st.user.name}! 👋")
        st.image(st.user.picture, width=50)
        st.button("Logout", on_click=st.logout)

        if st.toggle("Debug", value=False):
            # st.write(st.session_state.chat_history)
            st.write(st.user)
    # Main content area for HTML rendering
    with st.container():
        # Add deployment section at the top right
        col1, col2, col3 = st.columns([1, 2, 1])
        # Deploy button on the right
        with col1:
            if st.button("🐙 Push to GitHub", use_container_width=True):
                GH_TOKEN = get_github_token(st.user.sub)
                try:
                    create_new_repo(GH_TOKEN, st.session_state.app_name)
                except Exception as e:
                    print(f"Error pushing to GitHub: {e}")
                finally:
                    push_to_github(GH_TOKEN, st.user.nickname, st.session_state.app_name)
        with col3:
            if st.button("🚀 Deploy App", type="primary", use_container_width=True):
                try:
                    site, session_id = create_site(pat, team_slug, site_name=st.session_state.app_name)
                    st.session_state.session_id = session_id
                    st.session_state.site_url = site["url"]
                    zip_bytes = zip_from_html_str(st.session_state.html)
                    # deploy = deploy_zip_zipmethod(pat, site["id"], zip_bytes)
                    deploy = deploy_zip_buildapi(pat, site["id"], zip_bytes)
                    # Show success toast with link
                    st.toast(f"✅ Deployment successful! View your app at: [{site['url']}]({site['url']})", icon="🎉")
                except Exception as e:
                    # Show error toast
                    st.toast(f"❌ Deployment failed: {str(e)}", icon="⚠️")

        # Show app name and claim url on the left
        with col2:
            if "site_url" in st.session_state:
                st.markdown(f"**App Name:** [{st.session_state.app_name}]({st.session_state.site_url})")
            else:
                st.markdown(f"**App Name:** {st.session_state.app_name}")
            if "session_id" in st.session_state:
                claim_url = make_claim_link(
                oauth_client_id=st.secrets['NETLIFY_OAUTH_CLIENT_ID'],
                oauth_client_secret=st.secrets['NETLIFY_OAUTH_CLIENT_SECRET'],
                session_id=st.session_state.session_id,
            )
                st.markdown(f"**Claim the app ➡️:** [Click Here]({claim_url})")
        
    # Always render the HTML from session state
    st.components.v1.html(st.session_state.html, height=480, scrolling=True)