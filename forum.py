import sqlite3
import os
import sys
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import secrets
import pyotp
import qrcode
import io
import base64
import datetime

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

app.permanent_session_lifetime = datetime.timedelta(days=30)

UPLOAD_FOLDER = 'forum_uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/logo.png')
def serve_logo():
    return send_from_directory(os.getcwd(), 'logo.png')

def get_db():
    conn = sqlite3.connect('forum.db')
    conn.row_factory = sqlite3.Row
    return conn

# === Инициализация БД ===
def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS forum_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            totp_secret TEXT NOT NULL,
            avatar_url TEXT DEFAULT '',
            cover_url TEXT DEFAULT '',
            last_nick_change TIMESTAMP DEFAULT '1970-01-01',
            role TEXT DEFAULT 'user',
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            color TEXT DEFAULT '#4a6cf7',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS forums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER DEFAULT 0,
            title TEXT NOT NULL,
            description TEXT,
            created_by INTEGER,
            access_level TEXT DEFAULT 'all',
            can_create_topics TEXT DEFAULT 'all',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES forums(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS forum_tags (
            forum_id INTEGER,
            tag_id INTEGER,
            PRIMARY KEY (forum_id, tag_id),
            FOREIGN KEY (forum_id) REFERENCES forums(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS forum_moderators (
            forum_id INTEGER,
            user_id INTEGER,
            role TEXT,
            PRIMARY KEY (forum_id, user_id),
            FOREIGN KEY (forum_id) REFERENCES forums(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES forum_users(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_titles (
            user_id INTEGER,
            tag_id INTEGER,
            PRIMARY KEY (user_id, tag_id),
            FOREIGN KEY (user_id) REFERENCES forum_users(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forum_id INTEGER,
            user_id INTEGER,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            is_closed INTEGER DEFAULT 0,
            reply_mode TEXT DEFAULT 'all',
            allowed_user_id INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (forum_id) REFERENCES forums(id),
            FOREIGN KEY (user_id) REFERENCES forum_users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER,
            user_id INTEGER,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (topic_id) REFERENCES topics(id),
            FOREIGN KEY (user_id) REFERENCES forum_users(id)
        )
    ''')
    conn.commit()
    conn.close()

    # Добавляем недостающие колонки в старых базах (если не пересоздали)
    try:
        conn = get_db()
        conn.execute("ALTER TABLE forums ADD COLUMN can_create_topics TEXT DEFAULT 'all'")
        conn.commit()
    except:
        pass
    conn.close()

init_db()

# === Вспомогательные функции для должностей ===
def get_user_titles(user_id):
    conn = get_db()
    titles = conn.execute('''
        SELECT t.id, t.name, t.color 
        FROM user_titles ut
        JOIN tags t ON ut.tag_id = t.id
        WHERE ut.user_id = ?
    ''', (user_id,)).fetchall()
    conn.close()
    return [dict(title) for title in titles]

def get_user_title_ids(user_id):
    conn = get_db()
    ids = conn.execute('SELECT tag_id FROM user_titles WHERE user_id = ?', (user_id,)).fetchall()
    conn.close()
    return [row['tag_id'] for row in ids]

def set_user_titles(user_id, tag_ids):
    conn = get_db()
    conn.execute('DELETE FROM user_titles WHERE user_id = ?', (user_id,))
    for tag_id in tag_ids:
        conn.execute('INSERT INTO user_titles (user_id, tag_id) VALUES (?, ?)', (user_id, tag_id))
    conn.commit()
    conn.close()

# === Обновление последней активности ===
@app.before_request
def update_last_seen():
    if 'user_id' in session:
        conn = get_db()
        conn.execute("UPDATE forum_users SET last_seen = CURRENT_TIMESTAMP WHERE id = ?", (session['user_id'],))
        conn.commit()
        conn.close()

# === Вспомогательная функция для рендеринга ===
def render_with_base(content_html, **kwargs):
    base_html = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SENTRYIO FORUM</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { 
            background: linear-gradient(135deg, #03050a 0%, #0b132b 50%, #1a2035 100%); 
            background-attachment: fixed;
            min-height: 100vh; 
            display: flex; 
            flex-direction: column; 
            color: white; 
        }
        .navbar { 
            background: rgba(8, 10, 20, 0.8); 
            backdrop-filter: blur(10px);
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }
        .navbar-brand { 
            font-size: 1.5rem; 
            font-weight: bold; 
            letter-spacing: 2px; 
            color: white !important; 
            display: flex; 
            align-items: center; 
            gap: 15px;
        }
        .nav-link { color: rgba(255, 255, 255, 0.7) !important; }
        .nav-link:hover { color: white !important; }
        .card, .form-control {
            background: rgba(20, 25, 45, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.15);
            color: white;
        }
        .form-control:focus { border-color: #4a6cf7; }
        .btn-primary { background: #4a6cf7; border: none; border-radius: 50px; padding: 10px 30px; }
        .btn-outline-primary { color: #4a6cf7; border-color: #4a6cf7; border-radius: 50px; }
        .list-group-item {
            background: rgba(20, 25, 45, 0.6);
            border-color: rgba(255, 255, 255, 0.1);
            color: white;
        }
        .admin-card {
            background: rgba(20, 25, 45, 0.8);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 15px;
            padding: 20px;
        }
        footer { margin-top: auto; text-align: center; padding: 20px; color: rgba(255, 255, 255, 0.5); }
        a { color: #4a6cf7; text-decoration: none; }
        a:hover { color: white; }
        .tag-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
            margin: 2px 4px 2px 0;
            color: white;
        }
        .role-badge {
            display: inline-block;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 600;
            margin: 2px 4px 2px 0;
            color: white;
        }
        .role-badge.admin { background: #dc3545; }
        .role-badge.moderator { background: #fd7e14; }
        .role-badge.forum { background: #0d6efd; }
        .profile-card {
            position: relative;
            width: 100%;
            min-height: 400px;
            background-image: url('{{ cover or "/logo.png" }}');
            background-size: cover;
            background-position: center;
            border-radius: 20px;
            overflow: hidden;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        .profile-bg {
            position: absolute;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(10, 15, 30, 0.7);
            backdrop-filter: blur(4px);
            padding: 30px;
            display: flex;
            align-items: center;
        }
        .avatar-container {
            flex-shrink: 0;
            margin-right: 30px;
            z-index: 2;
        }
        .avatar-img {
            width: 150px; height: 150px;
            border-radius: 50%;
            border: 4px solid #4a6cf7;
            object-fit: cover;
            background: #1a2035;
        }
        .profile-info {
            z-index: 2;
            width: 100%;
        }
        .profile-info h2 { color: white; font-weight: bold; }
        .profile-info p { color: rgba(255,255,255,0.8); }
        .profile-info hr { border-color: rgba(255,255,255,0.2); }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark">
        <div class="container">
            <a class="navbar-brand" href="/">
                <img src="/logo.png" style="height: 60px; width: auto;">
                SENTRYIO FORUM
            </a>
            <div class="navbar-nav ms-auto">
                {% if session.username %}
                    <a class="nav-link" href="/profile">Профиль: {{ session.username }}</a>
                    {% if session.role == 'admin' %}
                    <a class="nav-link" href="/admin">Админ-панель</a>
                    {% endif %}
                    <a class="nav-link" href="/logout">Выход</a>
                {% else %}
                    <a class="nav-link" href="/login">Вход</a>
                    <a class="nav-link" href="/register">Регистрация</a>
                {% endif %}
            </div>
        </div>
    </nav>
    <div class="container mt-5 mb-5">
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% if messages %}
                {% for category, message in messages %}
                    <div class="alert alert-{{ category }} alert-dismissible fade show">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        {{ content|safe }}
    </div>
    <footer>&copy; 2026 SENTRYIO FORUM. All rights reserved.</footer>
</body>
</html>
    '''
    rendered_content = render_template_string(content_html, **kwargs) if content_html else ''
    context = {'session': session, 'content': rendered_content}
    context.update(kwargs)
    return render_template_string(base_html, **context)

# === Проверка прав доступа к разделу ===
def can_access_forum(forum_id, user_id=None):
    if user_id is None:
        user_id = session.get('user_id')
    conn = get_db()
    forum = conn.execute('SELECT access_level FROM forums WHERE id = ?', (forum_id,)).fetchone()
    conn.close()
    if not forum:
        return False
    level = forum['access_level']
    if level == 'all':
        return True
    if level == 'registered' and user_id:
        return True
    if level == 'admin':
        if user_id:
            conn = get_db()
            role = conn.execute('SELECT role FROM forum_users WHERE id = ?', (user_id,)).fetchone()
            conn.close()
            return role and role['role'] == 'admin'
        return False
    return True

def can_create_topic_in_forum(forum_id, user_id=None):
    if user_id is None:
        user_id = session.get('user_id')
    if not user_id:
        return False
    conn = get_db()
    forum = conn.execute('SELECT can_create_topics FROM forums WHERE id = ?', (forum_id,)).fetchone()
    if not forum:
        conn.close()
        return False
    policy = forum['can_create_topics']
    if policy == 'all':
        conn.close()
        return True
    if policy == 'admins':
        role = conn.execute('SELECT role FROM forum_users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
        return role and role['role'] == 'admin'
    if policy == 'moderators':
        mod = conn.execute('SELECT 1 FROM forum_moderators WHERE forum_id = ? AND user_id = ?', (forum_id, user_id)).fetchone()
        conn.close()
        return bool(mod)
    if policy == 'none':
        conn.close()
        return False
    conn.close()
    return False

def can_manage_forum(forum_id, user_id=None):
    if user_id is None:
        user_id = session.get('user_id')
    if not user_id:
        return False
    conn = get_db()
    role = conn.execute('SELECT role FROM forum_users WHERE id = ?', (user_id,)).fetchone()
    if role and role['role'] in ('admin', 'moderator'):
        conn.close()
        return True
    mod = conn.execute('SELECT 1 FROM forum_moderators WHERE forum_id = ? AND user_id = ?', (forum_id, user_id)).fetchone()
    conn.close()
    return bool(mod)

# === Главная страница ===
@app.route('/')
def index():
    conn = get_db()
    forums = conn.execute('''
        SELECT f.*, 
        (SELECT COUNT(*) FROM topics WHERE forum_id = f.id) as topic_count,
        (SELECT GROUP_CONCAT(t.name, ',') FROM forum_tags ft JOIN tags t ON ft.tag_id = t.id WHERE ft.forum_id = f.id) as tags_list
        FROM forums f
        WHERE f.parent_id = 0
        ORDER BY f.created_at DESC
    ''').fetchall()
    conn.close()
    if not forums:
        content = '''
<div class="text-center mb-5">
    <h1 class="display-4 fw-bold">Добро пожаловать</h1>
    <p class="lead">Выберите раздел для обсуждения</p>
</div>
<div class="text-center mt-5">
    <div class="card p-5 mx-auto" style="max-width: 500px;">
        <div style="font-size: 4rem; margin-bottom: 20px;">📭</div>
        <h3>Здесь пока пусто...</h3>
        <p class="text-muted">Создайте первый раздел в админ-панели!</p>
        <a href="/admin" class="btn btn-primary">Перейти в админ-панель</a>
    </div>
</div>
        '''
    else:
        content = '''
<div class="text-center mb-5">
    <h1 class="display-4 fw-bold">Добро пожаловать</h1>
    <p class="lead">Выберите раздел для обсуждения</p>
</div>
<div class="row justify-content-center">
    {% for forum in forums %}
    <div class="col-12 col-md-6 col-lg-4 mb-4">
        <div class="card p-4 h-100">
            <h3><a href="/forum/{{ forum.id }}" class="text-decoration-none">💬 {{ forum.title }}</a></h3>
            <p>{{ forum.description }}</p>
            <div class="mb-2">
                {% if forum.tags_list %}
                    {% for tag in forum.tags_list.split(',') %}
                    <span class="tag-badge" style="background-color: #4a6cf7;">{{ tag }}</span>
                    {% endfor %}
                {% endif %}
            </div>
            <small class="text-muted">Тем: {{ forum.topic_count }} | Создан: {{ forum.created_at.split(' ')[0] }}</small>
        </div>
    </div>
    {% endfor %}
</div>
        '''
    return render_with_base(content, forums=forums)

# === Просмотр раздела ===
@app.route('/forum/<int:forum_id>')
def forum_view(forum_id):
    if not can_access_forum(forum_id):
        flash('У вас нет доступа к этому разделу.', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    forum = conn.execute('SELECT * FROM forums WHERE id = ?', (forum_id,)).fetchone()
    if not forum:
        flash('Раздел не найден!', 'danger')
        return redirect(url_for('index'))
    subforums = conn.execute('''
        SELECT f.*, 
        (SELECT COUNT(*) FROM topics WHERE forum_id = f.id) as topic_count
        FROM forums f
        WHERE f.parent_id = ?
        ORDER BY f.created_at DESC
    ''', (forum_id,)).fetchall()
    topics = conn.execute('''
        SELECT t.*, u.username
        FROM topics t 
        JOIN forum_users u ON t.user_id = u.id 
        WHERE t.forum_id = ? 
        ORDER BY t.created_at DESC
    ''', (forum_id,)).fetchall()
    
    # Обрабатываем темы, подгружая их должности
    topics_data = []
    for topic in topics:
        topic_dict = dict(topic)
        topic_dict['user_titles'] = get_user_titles(topic['user_id'])
        topics_data.append(topic_dict)
        
    conn.close()
    can_create = can_create_topic_in_forum(forum_id)
    content = f'''
<h2>{forum['title']}</h2>
<p>{forum['description']}</p>
{{% if subforums %}}
<h4>Подфорумы</h4>
<div class="row mb-4">
    {{% for sub in subforums %}}
    <div class="col-md-6 mb-3">
        <div class="card p-3">
            <h5><a href="{{{{ url_for('forum_view', forum_id=sub.id) }}}}">{{{{ sub.title }}}}</a></h5>
            <p class="small">{{{{ sub.description }}}}</p>
            <small class="text-muted">Тем: {{{{ sub.topic_count }}}}</small>
        </div>
    </div>
    {{% endfor %}}
</div>
{{% endif %}}
{{% if can_create %}}
<a href="{{{{ url_for('new_topic', forum_id={forum_id}) }}}}" class="btn btn-primary mb-3">Создать тему</a>
{{% else %}}
<p class="text-warning">У вас нет прав на создание тем в этом разделе.</p>
{{% endif %}}
<div class="list-group">
    {{% for topic in topics_data %}}
    <a href="{{{{ url_for('topic_view', topic_id=topic.id) }}}}" class="list-group-item list-group-item-action">
        <h5>{{{{ topic.title }}}}</h5>
        <small>
            Автор: {{{{ topic.username }}}} 
            {{% for title in topic.user_titles %}}
                <span class="role-badge" style="background-color: {{{{ title.color }}}};">{{{{ title.name }}}}</span>
            {{% endfor %}}
        </small>
    </a>
    {{% endfor %}}
</div>
<a href="/" class="btn btn-secondary mt-3">← Назад к разделам</a>
    '''
    return render_with_base(content, forum=forum, subforums=subforums, topics_data=topics_data, can_create=can_create)

# === Создание темы ===
@app.route('/topic/new/<int:forum_id>', methods=['GET', 'POST'])
def new_topic(forum_id):
    if 'user_id' not in session:
        flash('Необходимо войти!', 'danger')
        return redirect(url_for('login'))
    if not can_access_forum(forum_id):
        flash('У вас нет доступа к этому разделу.', 'danger')
        return redirect(url_for('index'))
    if not can_create_topic_in_forum(forum_id):
        flash('У вас нет прав на создание тем в этом разделе.', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    forum = conn.execute('SELECT * FROM forums WHERE id = ?', (forum_id,)).fetchone()
    if not forum:
        flash('Раздел не найден!', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        title = request.form['title']
        content = request.form['content']
        if title and content:
            conn.execute('INSERT INTO topics (forum_id, user_id, title, content) VALUES (?, ?, ?, ?)',
                         (forum_id, session['user_id'], title, content))
            conn.commit()
            conn.close()
            flash('Тема создана!', 'success')
            return redirect(url_for('forum_view', forum_id=forum_id))
        flash('Заполните все поля!', 'danger')
    conn.close()
    content = f'''
<h2>Новая тема в разделе «{forum['title']}»</h2>
<form method="POST">
    <div class="mb-3">
        <label>Заголовок</label>
        <input type="text" name="title" class="form-control" required>
    </div>
    <div class="mb-3">
        <label>Содержание</label>
        <textarea name="content" class="form-control" rows="6" required></textarea>
    </div>
    <button type="submit" class="btn btn-primary">Создать</button>
    <a href="{{{{ url_for('forum_view', forum_id={forum_id}) }}}}" class="btn btn-secondary">Отмена</a>
</form>
    '''
    return render_with_base(content)

# === Просмотр темы ===
@app.route('/topic/<int:topic_id>')
def topic_view(topic_id):
    conn = get_db()
    topic = conn.execute('''
        SELECT t.*, u.username, f.title as forum_title, f.id as forum_id
        FROM topics t
        JOIN forum_users u ON t.user_id = u.id
        JOIN forums f ON t.forum_id = f.id
        WHERE t.id = ?
    ''', (topic_id,)).fetchone()
    if not topic:
        flash('Тема не найдена!', 'danger')
        return redirect(url_for('index'))
    if not can_access_forum(topic['forum_id']):
        flash('У вас нет доступа к этому разделу.', 'danger')
        return redirect(url_for('index'))
    messages = conn.execute('''
        SELECT m.*, u.username, u.avatar_url
        FROM messages m
        JOIN forum_users u ON m.user_id = u.id
        WHERE m.topic_id = ?
        ORDER BY m.created_at ASC
    ''', (topic_id,)).fetchall()
    
    # Обрабатываем сообщения, подгружая их должности
    messages_data = []
    for msg in messages:
        msg_dict = dict(msg)
        msg_dict['user_titles'] = get_user_titles(msg['user_id'])
        messages_data.append(msg_dict)
        
    can_reply = False
    if 'user_id' in session:
        if topic['reply_mode'] == 'all':
            can_reply = True
        elif topic['reply_mode'] == 'user':
            can_reply = session['user_id'] == topic['allowed_user_id']
        elif topic['reply_mode'] == 'owner':
            can_reply = session['user_id'] == topic['user_id']
        elif topic['reply_mode'] == 'closed':
            can_reply = False
    is_owner = 'user_id' in session and session['user_id'] == topic['user_id']
    is_admin = session.get('role') == 'admin'
    can_manage = is_admin or can_manage_forum(topic['forum_id'])
    
    author_titles = get_user_titles(topic['user_id'])
    conn.close()
    
    content = f'''
<h2>{topic['title']}</h2>
<p class="text-muted">
    Автор: {topic['username']} 
    {{% for title in author_titles %}}
        <span class="role-badge" style="background-color: {{{{ title.color }}}};">{{{{ title.name }}}}</span>
    {{% endfor %}}
</p>
<p>Раздел: <a href="{{{{ url_for('forum_view', forum_id={topic['forum_id']}) }}}}">{topic['forum_title']}</a></p>
<div class="mb-4">
    {topic['content']}
</div>
<hr>
<h4>Ответы</h4>
<div class="mb-4">
    {{% for msg in messages_data %}}
    <div class="card mb-2 p-3">
        <div class="d-flex justify-content-between">
            <div>
                <strong>{{{{ msg.username }}}}</strong>
                {{% for title in msg.user_titles %}}
                    <span class="role-badge" style="background-color: {{{{ title.color }}}};">{{{{ title.name }}}}</span>
                {{% endfor %}}
            </div>
            <small class="text-muted">{{{{ msg.created_at }}}}</small>
        </div>
        <p>{{{{ msg.content }}}}</p>
    </div>
    {{% endfor %}}
</div>

{{% if can_manage or is_owner %}}
<div class="row mt-3">
    <div class="col-12">
        <div class="card p-3">
            <h5>Управление темой</h5>
            <div class="d-flex flex-wrap gap-2">
                <a href="{{{{ url_for('topic_set_reply', topic_id={topic_id}, mode='all') }}}}" class="btn btn-sm btn-outline-primary">Все могут отвечать</a>
                <a href="{{{{ url_for('topic_set_reply', topic_id={topic_id}, mode='owner') }}}}" class="btn btn-sm btn-outline-secondary">Только я</a>
                <a href="{{{{ url_for('topic_set_reply', topic_id={topic_id}, mode='user', user_id=0) }}}}" class="btn btn-sm btn-outline-warning">Только указанный</a>
                <a href="{{{{ url_for('topic_set_reply', topic_id={topic_id}, mode='closed') }}}}" class="btn btn-sm btn-outline-danger">Закрыть ответы</a>
                <a href="{{{{ url_for('topic_delete', topic_id={topic_id}) }}}}" class="btn btn-sm btn-danger" onclick="return confirm('Удалить тему?')">Удалить тему</a>
            </div>
            <p class="small mt-2">Режим: 
                {{% if topic.reply_mode == 'all' %}}Все{{% elif topic.reply_mode == 'owner' %}}Только автор{{% elif topic.reply_mode == 'user' %}}Только указанный{{% elif topic.reply_mode == 'closed' %}}Закрыто{{% endif %}}
            </p>
        </div>
    </div>
</div>
{{% endif %}}

{{% if can_reply %}}
<form method="POST" action="{{{{ url_for('reply_topic', topic_id={topic_id}) }}}}">
    <div class="mb-3">
        <textarea name="content" class="form-control" rows="4" placeholder="Ваш ответ..." required></textarea>
    </div>
    <button type="submit" class="btn btn-primary">Ответить</button>
</form>
{{% elif topic.reply_mode == 'closed' %}}
<div class="alert alert-warning">Эта тема закрыта для ответов.</div>
{{% else %}}
<div class="alert alert-info">У вас нет прав для ответа в этой теме.</div>
{{% endif %}}
<a href="{{{{ url_for('forum_view', forum_id={topic['forum_id']}) }}}}" class="btn btn-secondary">← К разделу</a>
    '''
    return render_with_base(content, topic=topic, messages_data=messages_data, author_titles=author_titles, can_reply=can_reply, is_owner=is_owner, is_admin=is_admin, can_manage=can_manage)

# === Ответ на тему ===
@app.route('/topic/<int:topic_id>/reply', methods=['POST'])
def reply_topic(topic_id):
    if 'user_id' not in session:
        flash('Необходимо войти!', 'danger')
        return redirect(url_for('login'))
    conn = get_db()
    topic = conn.execute('SELECT reply_mode, allowed_user_id, user_id FROM topics WHERE id = ?', (topic_id,)).fetchone()
    if not topic:
        flash('Тема не найдена!', 'danger')
        return redirect(url_for('index'))
    can_reply = False
    mode = topic['reply_mode']
    if mode == 'all':
        can_reply = True
    elif mode == 'owner':
        can_reply = session['user_id'] == topic['user_id']
    elif mode == 'user':
        can_reply = session['user_id'] == topic['allowed_user_id']
    elif mode == 'closed':
        can_reply = False
    if not can_reply:
        flash('У вас нет прав для ответа в этой теме.', 'danger')
        return redirect(url_for('topic_view', topic_id=topic_id))
    content = request.form['content']
    if content:
        conn.execute('INSERT INTO messages (topic_id, user_id, content) VALUES (?, ?, ?)',
                     (topic_id, session['user_id'], content))
        conn.commit()
        conn.close()
        flash('Ответ отправлен!', 'success')
    else:
        flash('Сообщение не может быть пустым!', 'danger')
    return redirect(url_for('topic_view', topic_id=topic_id))

# === Управление правами на ответ ===
@app.route('/topic/<int:topic_id>/set_reply/<mode>', methods=['GET'])
def topic_set_reply(topic_id, mode):
    if 'user_id' not in session:
        flash('Необходимо войти!', 'danger')
        return redirect(url_for('login'))
    conn = get_db()
    topic = conn.execute('SELECT user_id, reply_mode FROM topics WHERE id = ?', (topic_id,)).fetchone()
    if not topic:
        flash('Тема не найдена!', 'danger')
        return redirect(url_for('index'))
    is_owner = session['user_id'] == topic['user_id']
    is_admin = session.get('role') == 'admin'
    can_manage = is_admin or can_manage_forum(topic['forum_id'])
    if not (is_owner or can_manage):
        flash('У вас нет прав на управление этой темой.', 'danger')
        return redirect(url_for('topic_view', topic_id=topic_id))
    
    if mode == 'all':
        conn.execute("UPDATE topics SET reply_mode = 'all', allowed_user_id = 0 WHERE id = ?", (topic_id,))
        flash('Режим изменён: все могут отвечать.', 'success')
    elif mode == 'owner':
        conn.execute("UPDATE topics SET reply_mode = 'owner', allowed_user_id = 0 WHERE id = ?", (topic_id,))
        flash('Режим изменён: только автор может отвечать.', 'success')
    elif mode == 'user':
        user_id_param = request.args.get('user_id')
        if not user_id_param or not user_id_param.isdigit():
            flash('Укажите ID через ?user_id=123', 'danger')
            conn.close()
            return redirect(url_for('topic_view', topic_id=topic_id))
        allowed_id = int(user_id_param)
        conn.execute("UPDATE topics SET reply_mode = 'user', allowed_user_id = ? WHERE id = ?", (allowed_id, topic_id))
        flash(f'Режим изменён: только пользователь ID {allowed_id} может отвечать.', 'success')
    elif mode == 'closed':
        conn.execute("UPDATE topics SET reply_mode = 'closed', allowed_user_id = 0 WHERE id = ?", (topic_id,))
        flash('Режим изменён: тема закрыта для ответов.', 'success')
    else:
        flash('Неизвестный режим.', 'danger')
    conn.commit()
    conn.close()
    return redirect(url_for('topic_view', topic_id=topic_id))

# === Удаление темы ===
@app.route('/topic/<int:topic_id>/delete')
def topic_delete(topic_id):
    if 'user_id' not in session:
        flash('Необходимо войти!', 'danger')
        return redirect(url_for('login'))
    conn = get_db()
    topic = conn.execute('SELECT user_id, forum_id FROM topics WHERE id = ?', (topic_id,)).fetchone()
    if not topic:
        flash('Тема не найдена!', 'danger')
        return redirect(url_for('index'))
    is_owner = session['user_id'] == topic['user_id']
    is_admin = session.get('role') == 'admin'
    can_manage = is_admin or can_manage_forum(topic['forum_id'])
    if not (is_owner or can_manage):
        flash('У вас нет прав на удаление этой темы.', 'danger')
        return redirect(url_for('topic_view', topic_id=topic_id))
    forum_id = topic['forum_id']
    conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    conn.commit()
    conn.close()
    flash('Тема удалена!', 'success')
    return redirect(url_for('forum_view', forum_id=forum_id))

# === Админ-панель ===
def is_admin():
    if 'user_id' not in session:
        return False
    conn = get_db()
    role = conn.execute('SELECT role FROM forum_users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    return role and role['role'] == 'admin'

@app.route('/admin')
def admin_panel():
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    
    search_query = request.args.get('search', '').strip()
    conn = get_db()
    forums = conn.execute('SELECT * FROM forums ORDER BY parent_id, created_at DESC').fetchall()
    tags = conn.execute('SELECT * FROM tags ORDER BY created_at DESC').fetchall()
    
    if search_query:
        users = conn.execute('''
            SELECT id, username, role, last_seen, created_at 
            FROM forum_users 
            WHERE username LIKE ? 
            ORDER BY id DESC
        ''', (f'%{search_query}%',)).fetchall()
    else:
        users = conn.execute('''
            SELECT id, username, role, last_seen, created_at 
            FROM forum_users 
            ORDER BY id DESC
        ''').fetchall()
        
    mods = {}
    for forum in forums:
        mods[forum['id']] = conn.execute('''
            SELECT u.id, u.username, fm.role 
            FROM forum_moderators fm
            JOIN forum_users u ON fm.user_id = u.id
            WHERE fm.forum_id = ?
        ''', (forum['id'],)).fetchall()
    conn.close()
    
    content = f'''
<h2 class="mb-4">🔧 Админ-панель</h2>
<div class="row">
    <div class="col-md-6 mb-4">
        <div class="admin-card">
            <h4>Управление разделами</h4>
            <a href="/admin/forums/new" class="btn btn-primary btn-sm">Создать раздел</a>
            <ul class="list-group mt-3">
                {{% for f in forums %}}
                <li class="list-group-item d-flex justify-content-between align-items-center">
                    <div>
                        <strong>{{{{ f.title }}}}</strong>
                        <br><small>ID: {{{{ f.id }}}} | Родитель: {{{{ f.parent_id }}}} | Доступ: {{{{ f.access_level }}}}</small>
                        <br><small>Модераторы: 
                            {{% for m in mods[f.id] %}}
                                <span class="role-badge {{ 'moderator' if m.role=='moderator' else 'curator' }}">{{{{ m.username }}}} ({{{{ m.role }}}})</span>
                            {{% endfor %}}
                        </small>
                    </div>
                    <div>
                        <a href="/admin/forums/edit/{{{{ f.id }}}}" class="btn btn-secondary btn-sm">✏️</a>
                        <a href="/admin/forums/delete/{{{{ f.id }}}}" class="btn btn-danger btn-sm" onclick="return confirm('Удалить раздел?')">🗑️</a>
                    </div>
                </li>
                {{% endfor %}}
            </ul>
        </div>
    </div>
    <div class="col-md-6 mb-4">
        <div class="admin-card">
            <h4>Управление тегами</h4>
            <a href="/admin/tags/new" class="btn btn-primary btn-sm">Создать тег</a>
            <ul class="list-group mt-3">
                {{% for tag in tags %}}
                <li class="list-group-item d-flex justify-content-between align-items-center">
                    <span class="tag-badge" style="background-color: {{{{ tag.color }}}};">{{{{ tag.name }}}}</span>
                    <div>
                        <a href="/admin/tags/delete/{{{{ tag.id }}}}" class="btn btn-danger btn-sm" onclick="return confirm('Удалить тег?')">🗑️</a>
                    </div>
                </li>
                {{% endfor %}}
            </ul>
        </div>
    </div>
</div>
<div class="row mt-4">
    <div class="col-12">
        <div class="admin-card">
            <h4>Управление пользователями</h4>
            
            <!-- Поиск -->
            <form method="GET" action="/admin" class="mb-3">
                <div class="input-group">
                    <input type="text" name="search" class="form-control" placeholder="Поиск по нику..." value="{{ search_query if search_query else '' }}">
                    <button type="submit" class="btn btn-primary">Найти</button>
                    {{% if search_query %}}
                    <a href="/admin" class="btn btn-secondary">Сбросить</a>
                    {{% endif %}}
                </div>
            </form>

            <table class="table table-dark table-striped">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Никнейм</th>
                        <th>Роль</th>
                        <th>Должности (теги)</th>
                        <th>Последний визит</th>
                        <th>Действия</th>
                    </tr>
                </thead>
                <tbody>
                    {{% for u in users %}}
                    <tr>
                        <td>{{{{ u.id }}}}</td>
                        <td><a href="/profile/{{{{ u.id }}}}" class="text-decoration-none text-info">{{{{ u.username }}}}</a></td>
                        <td>
                            <form method="POST" action="/admin/users/role/{{{{ u.id }}}}" class="d-inline">
                                <select name="role" class="form-select form-select-sm d-inline" style="width: auto;">
                                    <option value="user" {{% if u.role == 'user' %}}selected{{% endif %}}>User</option>
                                    <option value="moderator" {{% if u.role == 'moderator' %}}selected{{% endif %}}>Модератор</option>
                                    <option value="admin" {{% if u.role == 'admin' %}}selected{{% endif %}}>Admin</option>
                                </select>
                                <button type="submit" class="btn btn-primary btn-sm">Изменить</button>
                            </form>
                        </td>
                        <td>
                            {{% set user_title_ids = get_user_title_ids(u.id) %}}
                            <form method="POST" action="/admin/users/titles/{{{{ u.id }}}}" class="d-inline">
                                <div class="d-flex flex-wrap gap-2" style="max-width: 300px;">
                                    {{% for tag in tags %}}
                                    <div class="form-check form-check-inline">
                                        <input class="form-check-input" type="checkbox" name="titles" value="{{{{ tag.id }}}}" {{% if tag.id in user_title_ids %}}checked{{% endif %}}>
                                        <label class="form-check-label" style="color: {{{{ tag.color }}}}; font-size: 0.8rem;">{{{{ tag.name }}}}</label>
                                    </div>
                                    {{% endfor %}}
                                </div>
                                <button type="submit" class="btn btn-secondary btn-sm mt-1">Сохранить должности</button>
                            </form>
                        </td>
                        <td>{{{{ u.last_seen or 'Никогда' }}}}</td>
                        <td>
                            <a href="/profile/{{{{ u.id }}}}" class="btn btn-info btn-sm">Профиль</a>
                        </td>
                    </tr>
                    {{% endfor %}}
                </tbody>
            </table>
        </div>
    </div>
</div>
<a href="/" class="btn btn-secondary">← На главную</a>
    '''
    return render_with_base(content, forums=forums, tags=tags, users=users, mods=mods, search_query=search_query, get_user_title_ids=get_user_title_ids)

# === Создание/редактирование разделов ===
@app.route('/admin/forums/new', methods=['GET', 'POST'])
def admin_forums_new():
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    all_forums = conn.execute('SELECT id, title FROM forums').fetchall()
    all_tags = conn.execute('SELECT id, name FROM tags').fetchall()
    conn.close()
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        parent_id = int(request.form.get('parent_id', 0))
        access_level = request.form.get('access_level', 'all')
        can_create_topics = request.form.get('can_create_topics', 'all')
        selected_tags = request.form.getlist('tags')
        if title:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO forums (title, description, parent_id, access_level, can_create_topics, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (title, description, parent_id, access_level, can_create_topics, session['user_id']))
            forum_id = cursor.lastrowid
            for tag_id in selected_tags:
                cursor.execute('INSERT INTO forum_tags (forum_id, tag_id) VALUES (?, ?)', (forum_id, tag_id))
            conn.commit()
            conn.close()
            flash('Раздел создан!', 'success')
            return redirect(url_for('admin_panel'))
        flash('Название обязательно!', 'danger')
    content = f'''
<h2>Создать раздел</h2>
<form method="POST">
    <div class="mb-3">
        <label>Название</label>
        <input type="text" name="title" class="form-control" required>
    </div>
    <div class="mb-3">
        <label>Описание</label>
        <textarea name="description" class="form-control" rows="3"></textarea>
    </div>
    <div class="mb-3">
        <label>Родительский раздел</label>
        <select name="parent_id" class="form-control">
            <option value="0">Корневой</option>
            {{% for f in all_forums %}}
            <option value="{{{{ f.id }}}}">{{{{ f.title }}}}</option>
            {{% endfor %}}
        </select>
    </div>
    <div class="mb-3">
        <label>Кто может создавать темы</label>
        <select name="can_create_topics" class="form-control">
            <option value="all">Все</option>
            <option value="admins">Только админы</option>
            <option value="moderators">Только модераторы/кураторы этого раздела</option>
            <option value="none">Никто</option>
        </select>
    </div>
    <div class="mb-3">
        <label>Уровень доступа</label>
        <select name="access_level" class="form-control">
            <option value="all">Все</option>
            <option value="registered">Только зарегистрированные</option>
            <option value="admin">Только админы</option>
        </select>
    </div>
    <div class="mb-3">
        <label>Теги</label>
        <div>
            {{% for tag in all_tags %}}
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="checkbox" name="tags" value="{{{{ tag.id }}}}">
                <label class="form-check-label">{{{{ tag.name }}}}</label>
            </div>
            {{% endfor %}}
        </div>
    </div>
    <button type="submit" class="btn btn-primary">Создать</button>
    <a href="/admin" class="btn btn-secondary">Отмена</a>
</form>
    '''
    return render_with_base(content, all_forums=all_forums, all_tags=all_tags)

@app.route('/admin/forums/edit/<int:forum_id>', methods=['GET', 'POST'])
def admin_forums_edit(forum_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    forum = conn.execute('SELECT * FROM forums WHERE id = ?', (forum_id,)).fetchone()
    if not forum:
        flash('Раздел не найден!', 'danger')
        return redirect(url_for('admin_panel'))
    all_forums = conn.execute('SELECT id, title FROM forums WHERE id != ?', (forum_id,)).fetchall()
    all_tags = conn.execute('SELECT id, name FROM tags').fetchall()
    current_tags = conn.execute('SELECT tag_id FROM forum_tags WHERE forum_id = ?', (forum_id,)).fetchall()
    current_tag_ids = [row['tag_id'] for row in current_tags]
    conn.close()
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        parent_id = int(request.form.get('parent_id', 0))
        access_level = request.form.get('access_level', 'all')
        can_create_topics = request.form.get('can_create_topics', 'all')
        selected_tags = request.form.getlist('tags')
        if title:
            conn = get_db()
            conn.execute('''
                UPDATE forums SET title = ?, description = ?, parent_id = ?, access_level = ?, can_create_topics = ?
                WHERE id = ?
            ''', (title, description, parent_id, access_level, can_create_topics, forum_id))
            conn.execute('DELETE FROM forum_tags WHERE forum_id = ?', (forum_id,))
            for tag_id in selected_tags:
                conn.execute('INSERT INTO forum_tags (forum_id, tag_id) VALUES (?, ?)', (forum_id, tag_id))
            conn.commit()
            conn.close()
            flash('Раздел обновлён!', 'success')
            return redirect(url_for('admin_panel'))
        flash('Название обязательно!', 'danger')
    content = f'''
<h2>Редактировать раздел «{forum['title']}»</h2>
<form method="POST">
    <div class="mb-3">
        <label>Название</label>
        <input type="text" name="title" class="form-control" value="{forum['title']}" required>
    </div>
    <div class="mb-3">
        <label>Описание</label>
        <textarea name="description" class="form-control" rows="3">{forum['description']}</textarea>
    </div>
    <div class="mb-3">
        <label>Родительский раздел</label>
        <select name="parent_id" class="form-control">
            <option value="0">Корневой</option>
            {{% for f in all_forums %}}
            <option value="{{{{ f.id }}}}" {{% if f.id == {forum['parent_id']} %}}selected{{% endif %}}>{{{{ f.title }}}}</option>
            {{% endfor %}}
        </select>
    </div>
    <div class="mb-3">
        <label>Кто может создавать темы</label>
        <select name="can_create_topics" class="form-control">
            <option value="all" {{% if forum['can_create_topics'] == 'all' %}}selected{{% endif %}}>Все</option>
            <option value="admins" {{% if forum['can_create_topics'] == 'admins' %}}selected{{% endif %}}>Только админы</option>
            <option value="moderators" {{% if forum['can_create_topics'] == 'moderators' %}}selected{{% endif %}}>Только модераторы этого раздела</option>
            <option value="none" {{% if forum['can_create_topics'] == 'none' %}}selected{{% endif %}}>Никто</option>
        </select>
    </div>
    <div class="mb-3">
        <label>Уровень доступа</label>
        <select name="access_level" class="form-control">
            <option value="all" {{% if forum['access_level'] == 'all' %}}selected{{% endif %}}>Все</option>
            <option value="registered" {{% if forum['access_level'] == 'registered' %}}selected{{% endif %}}>Только зарегистрированные</option>
            <option value="admin" {{% if forum['access_level'] == 'admin' %}}selected{{% endif %}}>Только админы</option>
        </select>
    </div>
    <div class="mb-3">
        <label>Теги</label>
        <div>
            {{% for tag in all_tags %}}
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="checkbox" name="tags" value="{{{{ tag.id }}}}" {{% if tag.id in current_tag_ids %}}checked{{% endif %}}>
                <label class="form-check-label">{{{{ tag.name }}}}</label>
            </div>
            {{% endfor %}}
        </div>
    </div>
    <button type="submit" class="btn btn-primary">Сохранить</button>
    <a href="/admin" class="btn btn-secondary">Отмена</a>
</form>
    '''
    return render_with_base(content, forum=forum, all_forums=all_forums, all_tags=all_tags, current_tag_ids=current_tag_ids)

@app.route('/admin/forums/delete/<int:forum_id>')
def admin_forums_delete(forum_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    conn.execute('DELETE FROM forums WHERE id = ?', (forum_id,))
    conn.commit()
    conn.close()
    flash('Раздел удалён!', 'success')
    return redirect(url_for('admin_panel'))

# === Управление модераторами/кураторами ===
@app.route('/admin/forums/<int:forum_id>/moderators', methods=['GET', 'POST'])
def admin_forums_moderators(forum_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    forum = conn.execute('SELECT title FROM forums WHERE id = ?', (forum_id,)).fetchone()
    if not forum:
        flash('Раздел не найден!', 'danger')
        return redirect(url_for('admin_panel'))
    if request.method == 'POST':
        username = request.form.get('username')
        role = request.form.get('role', 'moderator')
        if username:
            user = conn.execute('SELECT id FROM forum_users WHERE username LIKE ?', (username,)).fetchone()
            if user:
                user_id = user['id']
                conn.execute('DELETE FROM forum_moderators WHERE forum_id = ? AND user_id = ?', (forum_id, user_id))
                conn.execute('INSERT INTO forum_moderators (forum_id, user_id, role) VALUES (?, ?, ?)', (forum_id, user_id, role))
                conn.commit()
                flash('Модератор/куратор добавлен!', 'success')
            else:
                flash('Пользователь не найден!', 'danger')
        else:
            flash('Укажите имя пользователя.', 'danger')
        return redirect(url_for('admin_forums_moderators', forum_id=forum_id))
    mods = conn.execute('''
        SELECT u.id, u.username, fm.role 
        FROM forum_moderators fm
        JOIN forum_users u ON fm.user_id = u.id
        WHERE fm.forum_id = ?
    ''', (forum_id,)).fetchall()
    conn.close()
    content = f'''
<h2>Управление модераторами раздела «{forum['title']}»</h2>
<form method="POST" class="mb-4">
    <div class="row">
        <div class="col-6">
            <input type="text" name="username" class="form-control" placeholder="Введите имя пользователя" required>
        </div>
        <div class="col-3">
            <select name="role" class="form-control">
                <option value="moderator">Модератор</option>
                <option value="curator">Куратор</option>
            </select>
        </div>
        <div class="col-3">
            <button type="submit" class="btn btn-primary">Добавить</button>
        </div>
    </div>
</form>
<h5>Текущие модераторы/кураторы:</h5>
<ul class="list-group">
    {{% for m in mods %}}
    <li class="list-group-item d-flex justify-content-between align-items-center">
        <span>{{{{ m.username }}}} ({{{{ m.role }}}})</span>
        <a href="/admin/forums/{forum_id}/moderators/delete/{{{{ m.id }}}}" class="btn btn-danger btn-sm" onclick="return confirm('Удалить?')">Удалить</a>
    </li>
    {{% endfor %}}
</ul>
<a href="/admin" class="btn btn-secondary mt-3">← Вернуться</a>
    '''
    return render_with_base(content, forum=forum, mods=mods)

@app.route('/admin/forums/<int:forum_id>/moderators/delete/<int:user_id>')
def admin_forums_moderators_delete(forum_id, user_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    conn.execute('DELETE FROM forum_moderators WHERE forum_id = ? AND user_id = ?', (forum_id, user_id))
    conn.commit()
    conn.close()
    flash('Модератор/куратор удалён.', 'success')
    return redirect(url_for('admin_forums_moderators', forum_id=forum_id))

# === Управление тегами ===
@app.route('/admin/tags/new', methods=['GET', 'POST'])
def admin_tags_new():
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form['name']
        color = request.form.get('color', '#4a6cf7')
        if name:
            conn = get_db()
            try:
                conn.execute('INSERT INTO tags (name, color) VALUES (?, ?)', (name, color))
                conn.commit()
                conn.close()
                flash('Тег создан!', 'success')
                return redirect(url_for('admin_panel'))
            except sqlite3.IntegrityError:
                flash('Тег с таким названием уже существует!', 'danger')
        else:
            flash('Название обязательно!', 'danger')
    content = '''
<h2>Создать тег</h2>
<form method="POST">
    <div class="mb-3">
        <label>Название тега</label>
        <input type="text" name="name" class="form-control" required>
    </div>
    <div class="mb-3">
        <label>Цвет (hex)</label>
        <input type="color" name="color" class="form-control form-control-color" value="#4a6cf7">
    </div>
    <button type="submit" class="btn btn-primary">Создать</button>
    <a href="/admin" class="btn btn-secondary">Отмена</a>
</form>
    '''
    return render_with_base(content)

@app.route('/admin/tags/delete/<int:tag_id>')
def admin_tags_delete(tag_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    conn = get_db()
    conn.execute('DELETE FROM tags WHERE id = ?', (tag_id,))
    conn.commit()
    conn.close()
    flash('Тег удалён!', 'success')
    return redirect(url_for('admin_panel'))

# === Управление пользователями ===
@app.route('/admin/users/role/<int:user_id>', methods=['POST'])
def admin_users_role(user_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    new_role = request.form['role']
    conn = get_db()
    conn.execute('UPDATE forum_users SET role = ? WHERE id = ?', (new_role, user_id))
    conn.commit()
    conn.close()
    flash('Роль обновлена!', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/users/titles/<int:user_id>', methods=['POST'])
def admin_users_titles(user_id):
    if not is_admin():
        flash('Доступ запрещён!', 'danger')
        return redirect(url_for('index'))
    selected_titles = request.form.getlist('titles')
    set_user_titles(user_id, selected_titles)
    flash('Должности обновлены!', 'success')
    return redirect(url_for('admin_panel'))

# === Профиль пользователя ===
@app.route('/profile')
@app.route('/profile/<int:user_id>')
def profile(user_id=None):
    if user_id is None:
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user_id = session['user_id']
    conn = get_db()
    user = conn.execute('''
        SELECT id, username, email, avatar_url, cover_url, last_nick_change, role, created_at, last_seen 
        FROM forum_users WHERE id = ?
    ''', (user_id,)).fetchone()
    conn.close()
    if not user:
        flash('Пользователь не найден!', 'danger')
        return redirect(url_for('index'))
    is_own = session.get('user_id') == user_id
    user_titles = get_user_titles(user_id)
    content = f'''
<div class="row justify-content-center">
    <div class="col-md-10">
        <div class="profile-card" style="background-image: url('{user['cover_url'] if user['cover_url'] else '/logo.png'}');">
            <div class="profile-bg">
                <div class="avatar-container">
                    <img src="{user['avatar_url'] if user['avatar_url'] else '/logo.png'}" class="avatar-img">
                </div>
                <div class="profile-info">
                    <h2>{user['username']}</h2>
                    {{% for title in user_titles %}}
                        <span class="role-badge" style="background-color: {{{{ title.color }}}};">{{{{ title.name }}}}</span>
                    {{% endfor %}}
                    <div style="background: rgba(0,0,0,0.4); padding: 15px; border-radius: 15px; backdrop-filter: blur(5px); margin-top: 10px;">
                        <p><strong>Email:</strong> {user['email']}</p>
                        <p><strong>Роль:</strong> {user['role']}</p>
                        <p><strong>Дата регистрации:</strong> {user['created_at'] if user['created_at'] else 'Неизвестно'}</p>
                        <p><strong>Последняя активность:</strong> {user['last_seen'] if user['last_seen'] else 'Никогда'}</p>
                        <p><strong>Последняя смена ника:</strong> {user['last_nick_change'] if user['last_nick_change'] != '1970-01-01' else 'Никогда'}</p>
                    </div>
                    {{% if is_own %}}
                    <div class="mt-3">
                        <a href="/profile/edit" class="btn btn-primary">Редактировать профиль</a>
                    </div>
                    {{% endif %}}
                </div>
            </div>
        </div>
    </div>
</div>
    '''
    return render_with_base(content, user=user, is_own=is_own, user_titles=user_titles)

# === Редактирование профиля ===
@app.route('/profile/edit', methods=['GET', 'POST'])
def profile_edit():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db()
    user = conn.execute('SELECT id, username, email, avatar_url, cover_url, last_nick_change FROM forum_users WHERE id = ?', (session['user_id'],)).fetchone()
    if request.method == 'POST':
        if 'new_username' in request.form:
            new_username = request.form['new_username'].strip()
            if new_username:
                last_change = user['last_nick_change']
                if last_change != '1970-01-01':
                    try:
                        last_date = datetime.datetime.strptime(last_change, '%Y-%m-%d %H:%M:%S')
                        if (datetime.datetime.now() - last_date).days < 30:
                            flash('Никнейм можно менять раз в 30 дней!', 'danger')
                            return redirect(url_for('profile'))
                    except: pass
                check_conn = get_db()
                existing = check_conn.execute('SELECT id FROM forum_users WHERE username = ?', (new_username,)).fetchone()
                check_conn.close()
                if existing and existing['id'] != session['user_id']:
                    flash('Никнейм занят!', 'danger')
                else:
                    conn.execute('UPDATE forum_users SET username = ?, last_nick_change = ? WHERE id = ?',
                                 (new_username, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), session['user_id']))
                    conn.commit()
                    session['username'] = new_username
                    flash('Никнейм обновлён!', 'success')
            else:
                flash('Никнейм не может быть пустым!', 'danger')
        if 'avatar_file' in request.files:
            file = request.files['avatar_file']
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = secure_filename(f"user_{session['user_id']}_avatar.{ext}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                avatar_url = f"/uploads/{filename}"
                conn.execute("UPDATE forum_users SET avatar_url = ? WHERE id = ?", (avatar_url, session['user_id']))
                conn.commit()
                flash('Аватарка обновлена!', 'success')
            elif file.filename:
                flash('Неверный формат файла! (png, jpg, gif, webp)', 'danger')
        if 'cover_file' in request.files:
            file = request.files['cover_file']
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = secure_filename(f"user_{session['user_id']}_cover.{ext}")
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                cover_url = f"/uploads/{filename}"
                conn.execute("UPDATE forum_users SET cover_url = ? WHERE id = ?", (cover_url, session['user_id']))
                conn.commit()
                session['cover_url'] = cover_url
                flash('Обложка обновлена!', 'success')
            elif file.filename:
                flash('Неверный формат файла! (png, jpg, gif, webp)', 'danger')
        conn.close()
        return redirect(url_for('profile'))
    conn.close()
    content = f'''
<div class="row justify-content-center">
    <div class="col-md-10">
        <h2>Редактирование профиля</h2>
        <div class="card p-4">
            <div class="row">
                <div class="col-md-6">
                    <h5>Сменить никнейм (раз в 30 дней)</h5>
                    <form method="POST">
                        <div class="input-group mb-3">
                            <input type="text" name="new_username" class="form-control" placeholder="Новый никнейм">
                            <button class="btn btn-primary" type="submit">Сменить</button>
                        </div>
                    </form>
                </div>
                <div class="col-md-6">
                    <h5>Загрузить аватарку</h5>
                    <form method="POST" enctype="multipart/form-data">
                        <div class="input-group">
                            <input type="file" name="avatar_file" accept="image/*" class="form-control">
                            <button class="btn btn-primary" type="submit">Загрузить</button>
                        </div>
                    </form>
                </div>
            </div>
            <div class="row mt-3">
                <div class="col-md-6">
                    <h5>Загрузить обложку</h5>
                    <form method="POST" enctype="multipart/form-data">
                        <div class="input-group">
                            <input type="file" name="cover_file" accept="image/*" class="form-control">
                            <button class="btn btn-primary" type="submit">Загрузить</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
        <a href="/profile" class="btn btn-secondary mt-3">← Назад к профилю</a>
    </div>
</div>
    '''
    return render_with_base(content)

# === Регистрация ===
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        confirm = request.form['confirm_password']
        if password != confirm:
            flash('Пароли не совпадают!', 'danger')
            return render_with_base(register_form())
        pwd_hash = generate_password_hash(password)
        totp_secret = pyotp.random_base32()
        conn = get_db()
        try:
            conn.execute('INSERT INTO forum_users (username, email, password_hash, totp_secret) VALUES (?, ?, ?, ?)',
                         (username, email, pwd_hash, totp_secret))
            conn.commit()
            totp = pyotp.TOTP(totp_secret)
            uri = totp.provisioning_uri(username, issuer_name="SENTRYIO Forum")
            qr = qrcode.make(uri)
            buffer = io.BytesIO()
            qr.save(buffer, format='PNG')
            buffer.seek(0)
            qr_base64 = base64.b64encode(buffer.read()).decode()
            content = f'''
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card p-4 text-center">
            <h3 class="text-primary">✅ Регистрация успешна!</h3>
            <p>Сохраните этот секретный ключ и добавьте его в Google Authenticator:</p>
            <div class="alert alert-info fs-4 fw-bold" style="background: rgba(74, 108, 247, 0.2); color: white; border-color: #4a6cf7;">{totp_secret}</div>
            <p>Или отсканируйте QR-код:</p>
            <img src="data:image/png;base64,{qr_base64}" class="img-fluid" style="max-width: 200px; border-radius: 15px; border: 1px solid rgba(255,255,255,0.3);">
        </div>
    </div>
</div>
            '''
            return render_with_base(content)
        except sqlite3.IntegrityError:
            flash('Никнейм или почта уже заняты!', 'danger')
        finally:
            conn.close()
    return render_with_base(register_form())

def register_form():
    return '''
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card p-4">
            <h3 class="text-center mb-4">📝 Регистрация</h3>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">Электронная почта</label>
                    <input type="email" name="email" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Никнейм</label>
                    <input type="text" name="username" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Пароль</label>
                    <input type="password" name="password" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Повторите пароль</label>
                    <input type="password" name="confirm_password" class="form-control" required>
                </div>
                <button type="submit" class="btn btn-primary w-100">Зарегистрироваться</button>
            </form>
        </div>
    </div>
</div>
    '''

# === Вход ===
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db()
        user = conn.execute('SELECT * FROM forum_users WHERE username = ?', (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password_hash'], password):
            session['pending_2fa_user'] = user['id']
            session['pending_2fa_username'] = user['username']
            session['totp_secret'] = user['totp_secret']
            return redirect(url_for('verify_2fa'))
        else:
            flash('Неверный логин или пароль!', 'danger')
    return render_with_base(login_form())

def login_form():
    return '''
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card p-4">
            <h3 class="text-center mb-4">🔐 Вход</h3>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">Никнейм</label>
                    <input type="text" name="username" class="form-control" required>
                </div>
                <div class="mb-3">
                    <label class="form-label">Пароль</label>
                    <input type="password" name="password" class="form-control" required>
                </div>
                <button type="submit" class="btn btn-primary w-100">Войти</button>
            </form>
        </div>
    </div>
</div>
    '''

# === 2FA ===
@app.route('/verify-2fa', methods=['GET', 'POST'])
def verify_2fa():
    if 'pending_2fa_user' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        otp = request.form['otp']
        secret = session['totp_secret']
        totp = pyotp.TOTP(secret)
        if totp.verify(otp):
            session.permanent = True
            session['user_id'] = session.pop('pending_2fa_user')
            session['username'] = session.pop('pending_2fa_username')
            session.pop('totp_secret', None)
            conn = get_db()
            user_role = conn.execute('SELECT role FROM forum_users WHERE id = ?', (session['user_id'],)).fetchone()
            session['role'] = user_role['role'] if user_role else 'user'
            conn.close()
            return redirect(url_for('index'))
        else:
            flash('Неверный код 2FA!', 'danger')
    return render_with_base(verify_form())

def verify_form():
    return '''
<div class="row justify-content-center">
    <div class="col-md-6">
        <div class="card p-4">
            <h3 class="text-center mb-4">🔑 Двухфакторная аутентификация</h3>
            <p class="text-center">Введите код из приложения Google Authenticator</p>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label">Код 2FA</label>
                    <input type="text" name="otp" class="form-control" placeholder="6 цифр" required>
                </div>
                <button type="submit" class="btn btn-primary w-100">Подтвердить</button>
            </form>
        </div>
    </div>
</div>
    '''

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# === Запуск ===
if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'promote_admin':
        username_to_promote = sys.argv[2]
        conn = get_db()
        conn.execute("UPDATE forum_users SET role = 'admin' WHERE username = ?", (username_to_promote,))
        conn.commit()
        if conn.total_changes > 0:
            print(f"✅ Пользователь '{username_to_promote}' назначен Администратором!")
        else:
            print(f"❌ Пользователь '{username_to_promote}' не найден.")
        conn.close()
        sys.exit(0)

    app.run(host='0.0.0.0', port=5000)