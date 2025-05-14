import streamlit as st
import openai
import re
import os
import shutil

st.set_page_config(page_title="volt", page_icon="⚡")
openai.api_key=st.secrets['OPENAI_API_KEY']

@st.cache_resource
def initialize_index_html():
    """Initialize index.html from default template once per session"""
    if os.path.exists('default_index.html'):
        shutil.copy2('default_index.html', 'index.html')
        return True
    return False

# Initialize index.html if needed
initialize_index_html()

if "chat_history" not in st.session_state:
    st.session_state.chat_history = [{"role": "system", "content": systemPrompt}]

if "html_version" not in st.session_state:
    st.session_state.html_version = 0
    
avatar = {'user': '⚡', 'assistant': '🤖'}
model = 'gpt-4.1-mini'
st.logo('img/high-voltage.png')

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

def update_html_file(html_content):
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    st.session_state.html_version += 1

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

# Sidebar for chat interface
with st.sidebar:
    prompt = st.chat_input("Enter your message here", key="chat_input")
    messages = st.container(height=450)
    # Display chat history
    for message in st.session_state.chat_history:
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
        # Check for HTML content and update file if found
        html_content = extract_html_from_markdown(response)
        if html_content:
            print("Found HTML content, updating file...")  # Debug print
            update_html_file(html_content)
        st.write(f"HTML Version: {st.session_state.html_version}")
    
    # Add reset button at the bottom of the sidebar
    if st.button("New App", type="primary"):
        # Clear the chat history
        st.session_state.chat_history = []
        st.session_state.html_version = 0
        # Clear the cache
        st.cache_resource.clear()
        # Reinitialize index.html
        initialize_index_html()
        st.rerun()

    if st.checkbox("Debug", value=False):
        st.write(st.session_state.chat_history)

# Main content area for HTML rendering
with st.container():
    # Always render the index.html file
    with open('index.html', 'r', encoding='utf-8') as f:
        html_content = f.read()
    st.components.v1.html(html_content, height=500, scrolling=True)