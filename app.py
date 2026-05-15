from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import os
from optimizer import generar_horario_semana 
from datetime import datetime, timedelta
from sqlalchemy import inspect, text

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_key')

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    DATABASE_URL = 'sqlite:///instance/abicofarm.db'

# Carpeta para SQLite relativo (evita "unable to open database file" al ejecutar python app.py)
if DATABASE_URL.startswith('sqlite:') and ':memory:' not in DATABASE_URL:
    rest = DATABASE_URL
    for prefix in ('sqlite:///', 'sqlite:////'):
        if rest.startswith(prefix):
            rest = rest[len(prefix) :]
            break
    if rest and not rest.startswith(':'):
        db_path = os.path.abspath(rest)
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

# Render a veces entrega la URL con 'postgres://', SQLAlchemy necesita 'postgresql://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Supabase requiere SSL; agrega sslmode si no viene en la URL
if "supabase.co" in DATABASE_URL and "sslmode=" not in DATABASE_URL:
    sep = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{sep}sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)


def es_registro_administrativo_msg(mensaje_admin):
    """Suspensión / falta cargada por el admin (no es un permiso del empleado)."""
    m = (mensaje_admin or '').strip()
    return m == 'Registro Administrativo Directo' or m.startswith('Registro Administrativo Directo ·')


# --- MODELOS DE BASE DE DATOS ---
class Farmacia(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100))
    jornada = db.Column(db.String(100))

class Empleado(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100))
    rol = db.Column(db.String(50))
    horario = db.Column(db.String(100))
    username = db.Column(db.String(50), unique=True)
    password = db.Column(db.String(255))
    ultimo_login = db.Column(db.DateTime, nullable=True)
    activo_ahora = db.Column(db.Boolean, default=False, server_default='false')
    forzar_logout = db.Column(db.Boolean, default=False, server_default='false')
    
    # NUEVO: Día fijo de descanso (0=Lunes, 6=Domingo)
    dia_descanso_fijo = db.Column(db.Integer, nullable=True) 

    farmacia_id = db.Column(db.Integer, db.ForeignKey('farmacia.id'), nullable=True)
    farmacia = db.relationship('Farmacia', backref=db.backref('empleados', lazy=True))

class HorarioGenerado(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dia = db.Column(db.Integer) # 0=Lunes, 1=Martes... 5=Sábado
    empleado_id = db.Column(db.Integer, db.ForeignKey('empleado.id'))
    farmacia_id = db.Column(db.Integer, db.ForeignKey('farmacia.id'))
    
    empleado = db.relationship('Empleado', foreign_keys=[empleado_id])
    farmacia = db.relationship('Farmacia')

class Solicitud(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empleado_id = db.Column(db.Integer, db.ForeignKey('empleado.id'))
    fecha = db.Column(db.String(20)) # Guardará "YYYY-MM-DD"
    motivo = db.Column(db.String(200))
    estado = db.Column(db.String(50), default='Pendiente') # Pendiente, Aprobada, Rechazada, Modificada
    mensaje_admin = db.Column(db.String(200), default='') # Para la contraoferta
    tipo_permiso = db.Column(db.String(20), default='dia_completo', server_default='dia_completo')
    hora_retorno = db.Column(db.String(10), nullable=True)
    cobertura_empleado_id = db.Column(db.Integer, db.ForeignKey('empleado.id'), nullable=True)
    # permiso | cambio_descanso | cancelacion_falta
    categoria = db.Column(db.String(32), default='permiso', server_default='permiso')
    dia_descanso_solicitado = db.Column(db.Integer, nullable=True)  # 0=Lun .. 6=Dom (solo cambio_descanso)
    estado_al_pedir_cancel = db.Column(db.String(50), nullable=True)
    nota_empleado = db.Column(db.String(300), nullable=True)

    empleado = db.relationship('Empleado', foreign_keys=[empleado_id])
    cobertura_empleado = db.relationship('Empleado', foreign_keys=[cobertura_empleado_id])


def categoria_solicitud(s):
    c = getattr(s, 'categoria', None) or 'permiso'
    c = str(c).strip()
    return c if c else 'permiso'


def solicitud_cuenta_como_ausencia_ia(s):
    """Ausencias que bloquean turno al solicitante en OR-Tools (excluye cambio de descanso / impugnación)."""
    if s.estado not in ('Aprobada', 'Modificada (Aprobada)'):
        return False
    if categoria_solicitud(s) in ('cambio_descanso', 'cancelacion_falta'):
        return False
    return True


def _aplicar_cobertura_permiso_manual(solicitud):
    """
    Quita el turno generado al solicitante ese día y asigna al reemplazo en la misma sucursal,
    además de registrar AsignacionTemporal para el motor OR-Tools.
    """
    try:
        dt = datetime.strptime(solicitud.fecha, '%Y-%m-%d')
        dia_semana = dt.weekday()
    except Exception:
        return False, 'Fecha inválida en la solicitud.'

    if not solicitud.cobertura_empleado_id:
        return False, 'Falta empleado de cobertura.'

    turno = HorarioGenerado.query.filter_by(empleado_id=solicitud.empleado_id, dia=dia_semana).first()
    farmacia_id = None
    if turno:
        farmacia_id = turno.farmacia_id
        db.session.delete(turno)

    emp = Empleado.query.get(solicitud.empleado_id)
    if farmacia_id is None and emp and emp.farmacia_id:
        farmacia_id = emp.farmacia_id

    if farmacia_id is None:
        return False, 'No se pudo determinar la sucursal a cubrir.'

    cob_id = solicitud.cobertura_empleado_id
    HorarioGenerado.query.filter_by(empleado_id=cob_id, dia=dia_semana).delete()
    db.session.add(HorarioGenerado(dia=dia_semana, empleado_id=cob_id, farmacia_id=farmacia_id))

    AsignacionTemporal.query.filter_by(empleado_id=cob_id, fecha=solicitud.fecha).delete()
    db.session.add(AsignacionTemporal(
        empleado_id=cob_id,
        farmacia_destino_id=farmacia_id,
        fecha=solicitud.fecha
    ))
    return True, None


class AsignacionTemporal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empleado_id = db.Column(db.Integer, db.ForeignKey('empleado.id'))
    farmacia_destino_id = db.Column(db.Integer, db.ForeignKey('farmacia.id'))
    fecha = db.Column(db.String(20)) # "YYYY-MM-DD"

# --- RUTAS DE LA APLICACIÓN ---
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = Empleado.query.filter_by(username=username).first()
        if user and validar_password(user, password):
            user.ultimo_login = datetime.utcnow()
            user.activo_ahora = True
            db.session.commit()
            session['user_id'] = user.id
            session['empleado_id'] = user.id
            session['rol'] = user.rol
            session['nombre'] = user.nombre
            session['username'] = user.username
            session['login_time'] = datetime.utcnow().isoformat()
            session.permanent = False
            if user.rol == 'Desarrollador' or user.username.startswith('dev_'):
                return redirect(url_for('dev_panel'))
            if user.rol == 'Admin':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('empleado_dashboard'))
        else:
            flash('Usuario o contraseña incorrectos', 'error')

    return render_template('login.html')


def acceso_dev():
    username = session.get('username', '')
    return session.get('rol') == 'Desarrollador' or username.startswith('dev_')


def validar_password(usuario, password_plano):
    password_bd = usuario.password

    # Caso 1: ya está hasheada con werkzeug
    if password_bd.startswith(('scrypt:', 'pbkdf2:', '$2b$', '$2a$')):
        return check_password_hash(password_bd, password_plano)

    # Caso 2: texto plano (legado)
    if password_plano == password_bd:
        # Migrar automáticamente a hash en este momento
        usuario.password = generate_password_hash(password_plano)
        db.session.commit()
        return True

    return False


def verificar_sesion_activa():
    empleado_id = session.get('empleado_id') or session.get('user_id')
    if not empleado_id:
        return redirect('/login')

    empleado = Empleado.query.get(empleado_id)
    if not empleado:
        return redirect('/login')

    if empleado.forzar_logout:
        empleado.forzar_logout = False
        empleado.activo_ahora = False
        db.session.commit()
        session.clear()
        flash('Tu sesión fue cerrada por el administrador del sistema.')
        return redirect('/login')

    return None


@app.before_request
def proteger_rutas_con_sesion():
    endpoint = request.endpoint or ''
    if endpoint in {'login', 'logout', 'static'}:
        return None

    if request.path.startswith(('/admin', '/empleado', '/dev')):
        resultado = verificar_sesion_activa()
        if resultado:
            return resultado

    return None


@app.context_processor
def inject_template_helpers():
    return {'es_registro_administrativo_msg': es_registro_administrativo_msg}


@app.route('/admin')
def admin_dashboard():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))

    empleados = Empleado.query.filter(
        Empleado.rol != 'Desarrollador'
    ).all()
    farmacias = Farmacia.query.all()
    return render_template('admin.html', empleados=empleados, farmacias=farmacias, nombre=session['nombre'])

@app.route('/admin/solicitudes')
def admin_solicitudes():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitudes = Solicitud.query.order_by(Solicitud.id.desc()).all()
    
    # Obtenemos a todos los empleados para el nuevo formulario
    empleados = Empleado.query.filter(Empleado.rol != 'Admin').all()
    
    return render_template('admin_solicitudes.html', 
                           solicitudes=solicitudes, 
                           empleados=empleados,
                           nombre=session.get('nombre'))


@app.route('/admin/solicitudes/estado')
def admin_solicitudes_estado():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return jsonify({'error': 'unauthorized'}), 401

    solicitudes = Solicitud.query.order_by(Solicitud.id.desc()).all()
    payload = []
    for s in solicitudes:
        payload.append({
            'id': s.id,
            'empleado': s.empleado.nombre if s.empleado else '-',
            'fecha': s.fecha,
            'motivo': s.motivo,
            'estado': s.estado,
            'mensaje_admin': s.mensaje_admin or '',
            'tipo_permiso': s.tipo_permiso or 'dia_completo',
            'hora_retorno': s.hora_retorno or '',
            'cobertura_empleado': s.cobertura_empleado.nombre if s.cobertura_empleado else '',
            'categoria': categoria_solicitud(s),
            'dia_descanso_solicitado': s.dia_descanso_solicitado,
            'nota_empleado': (s.nota_empleado or ''),
        })

    return jsonify({'solicitudes': payload})

# Ruta para que el Admin pueda cancelar directamente una suspensión
@app.route('/admin/solicitudes/forzar_cancelacion/<int:id>', methods=['POST'])
def admin_forzar_cancelacion(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    if solicitud and es_registro_administrativo_msg(solicitud.mensaje_admin):
        solicitud.estado = 'Cancelada'
        db.session.commit()
        flash('Suspensión administrativa cancelada exitosamente. Se recomienda ejecutar el Motor de IA para actualizar.', 'success')

    return redirect(url_for('admin_solicitudes'))

# Procesa el formulario de suspensión/ausencia del admin
@app.route('/admin/registrar_ausencia', methods=['POST'])
def registrar_ausencia_admin():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    empleado_id = request.form.get('empleado_id')
    fecha = request.form.get('fecha')
    motivo = request.form.get('motivo')
    
    # Lo guardamos directamente como Aprobada
    nueva_ausencia = Solicitud(
        empleado_id=empleado_id,
        fecha=fecha,
        motivo=motivo,
        estado='Aprobada',
        mensaje_admin='Registro Administrativo Directo'
    )
    
    db.session.add(nueva_ausencia)
    db.session.commit()
    
    return redirect(url_for('admin_solicitudes'))

@app.route('/admin/solicitudes/estado/<int:id>/<estado>')
def cambiar_estado_solicitud(id, estado):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))

    solicitud = Solicitud.query.get(id)
    if not solicitud or estado not in ['Aprobada', 'Rechazada']:
        return redirect(url_for('admin_solicitudes'))

    cat = categoria_solicitud(solicitud)

    if estado == 'Rechazada':
        solicitud.estado = estado
        db.session.commit()
        return redirect(url_for('admin_solicitudes'))

    # ---- Aprobar ----
    if cat == 'permiso':
        if not solicitud.cobertura_empleado_id:
            flash('Para aprobar un permiso debes indicar primero un reemplazo (usa Modificar).', 'error')
            return redirect(url_for('modificar_solicitud', id=id))
        if solicitud.tipo_permiso == 'parcial' and not solicitud.hora_retorno:
            flash('Permiso parcial sin hora de entrada: complétalo en Modificar.', 'error')
            return redirect(url_for('modificar_solicitud', id=id))

        ok, err = _aplicar_cobertura_permiso_manual(solicitud)
        if not ok:
            db.session.rollback()
            flash(err or 'No se pudo registrar la cobertura.', 'error')
            return redirect(url_for('admin_solicitudes'))

        solicitud.estado = 'Aprobada'
        db.session.commit()
        flash('Permiso aprobado con cobertura y asignación para la IA.', 'success')
        return redirect(url_for('admin_solicitudes'))

    if cat == 'cambio_descanso':
        d = solicitud.dia_descanso_solicitado
        if d is None or d < 0 or d > 6:
            flash('Solicitud de descanso incompleta.', 'error')
            return redirect(url_for('admin_solicitudes'))
        emp = Empleado.query.get(solicitud.empleado_id)
        if not emp:
            return redirect(url_for('admin_solicitudes'))
        dias_txt = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
        emp.dia_descanso_fijo = int(d)
        solicitud.estado = 'Aprobada'
        solicitud.mensaje_admin = f'Descanso fijo: {dias_txt[d]} (la IA lo respetará al regenerar).'
        db.session.commit()
        flash('Cambio de día de descanso aplicado. Ejecuta OR-Tools para actualizar la semana.', 'success')
        return redirect(url_for('admin_solicitudes'))

    if cat == 'cancelacion_falta':
        sus = Solicitud.query.filter(
            Solicitud.id != solicitud.id,
            Solicitud.empleado_id == solicitud.empleado_id,
            Solicitud.fecha == solicitud.fecha,
            Solicitud.mensaje_admin.like('Registro Administrativo Directo%'),
            Solicitud.estado.in_(['Aprobada', 'Modificada (Aprobada)']),
        ).first()
        if not sus:
            flash('No hay falta administrativa activa para esa fecha.', 'error')
            return redirect(url_for('admin_solicitudes'))
        sus.estado = 'Cancelada'
        solicitud.estado = 'Aprobada'
        solicitud.mensaje_admin = 'Impugnación aceptada: falta administrativa anulada.'
        db.session.commit()
        flash('Falta administrativa anulada.', 'success')
        return redirect(url_for('admin_solicitudes'))

    flash('Esta categoría no se aprueba desde aquí.', 'error')
    return redirect(url_for('admin_solicitudes'))

@app.route('/admin/solicitudes/modificar/<int:id>', methods=['GET', 'POST'])
def modificar_solicitud(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    if not solicitud:
        return redirect(url_for('admin_solicitudes'))

    if categoria_solicitud(solicitud) != 'permiso':
        flash('Este tipo de solicitud se gestiona solo con Aprobar o Rechazar en la tabla.', 'info')
        return redirect(url_for('admin_solicitudes'))

    cobertura_opciones = Empleado.query.filter(Empleado.rol.in_(['Dependiente', 'Comodin'])).all()
    es_registro = es_registro_administrativo_msg(solicitud.mensaje_admin)

    if request.method == 'POST':
        solicitud.fecha = request.form['nueva_fecha']
        if es_registro:
            nota = (request.form.get('mensaje') or '').strip()
            if nota:
                solicitud.mensaje_admin = 'Registro Administrativo Directo · ' + nota[:160]
            db.session.commit()
            return redirect(url_for('admin_solicitudes'))

        solicitud.mensaje_admin = request.form['mensaje']
        cobertura_id = request.form.get('cobertura_empleado_id')

        if categoria_solicitud(solicitud) == 'permiso':
            if not cobertura_id:
                return render_template(
                    'solicitud_modificar.html',
                    solicitud=solicitud,
                    nombre=session['nombre'],
                    cobertura_opciones=cobertura_opciones,
                    error_cobertura='Debes seleccionar un reemplazo (dependiente o comodín).',
                    es_registro_admin=False,
                )
            solicitud.cobertura_empleado_id = int(cobertura_id)
            ok, err = _aplicar_cobertura_permiso_manual(solicitud)
            if not ok:
                db.session.rollback()
                solicitud = Solicitud.query.get(id)
                return render_template(
                    'solicitud_modificar.html',
                    solicitud=solicitud,
                    nombre=session['nombre'],
                    cobertura_opciones=cobertura_opciones,
                    error_cobertura=err or 'No se pudo aplicar la cobertura.',
                    es_registro_admin=False,
                )

        solicitud.estado = 'Modificada (Aprobada)' # Cuenta como aprobada pero con cambios
        db.session.commit()
        return redirect(url_for('admin_solicitudes'))
        
    return render_template(
        'solicitud_modificar.html',
        solicitud=solicitud,
        nombre=session['nombre'],
        cobertura_opciones=cobertura_opciones,
        error_cobertura=None,
        es_registro_admin=es_registro,
    )

@app.route('/empleado')
def empleado_dashboard():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return redirect(url_for('login'))

    return redirect(url_for('empleado_horario'))


@app.route('/empleado/horario')
def empleado_horario():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return redirect(url_for('login'))

    empleado_id = session['user_id']
    data = construir_estado_empleado(empleado_id)
    usuario_actual = Empleado.query.get(empleado_id)

    return render_template('empleado_horario.html',
                           nombre=session.get('nombre'),
                           user=usuario_actual,
                           mi_horario=data['mi_horario'],
                           fechas_semana=data['fechas_semana'])


@app.route('/empleado/permisos')
def empleado_permisos():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return redirect(url_for('login'))

    empleado_id = session['user_id']
    data = construir_estado_empleado(empleado_id)
    usuario_actual = Empleado.query.get(empleado_id)

    return render_template('empleado_permisos.html',
                           nombre=session.get('nombre'),
                           user=usuario_actual,
                           solicitudes=data['solicitudes'])


@app.route('/empleado/perfil')
def empleado_perfil():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return redirect(url_for('login'))

    empleado_id = session['user_id']
    usuario_actual = Empleado.query.get(empleado_id)
    farmacia = usuario_actual.farmacia.nombre if usuario_actual and usuario_actual.farmacia else '--'
    dias_nombre = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
    if usuario_actual and usuario_actual.dia_descanso_fijo is not None:
        dia_descanso_texto = dias_nombre[usuario_actual.dia_descanso_fijo]
    else:
        dia_descanso_texto = 'Rotativo'

    return render_template('perfil_empleado.html',
                           nombre=session.get('nombre'),
                           user=usuario_actual,
                           farmacia=farmacia,
                           dia_descanso_texto=dia_descanso_texto)


@app.route('/empleado/perfil/cambiar-credenciales', methods=['POST'])
def cambiar_credenciales_empleado():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    empleado_id = session['user_id']
    usuario_actual = Empleado.query.get(empleado_id)

    username_nuevo = request.form.get('username', '').strip()
    password_actual = request.form.get('password_actual', '')
    password_nueva = request.form.get('password_nueva', '')

    if not password_actual:
        return jsonify({'status': 'error', 'mensaje': 'Debes ingresar la contraseña actual.'}), 400

    if not validar_password(usuario_actual, password_actual):
        return jsonify({'status': 'error', 'mensaje': 'La contraseña actual no es valida.'}), 400

    if username_nuevo and username_nuevo != usuario_actual.username:
        existente = Empleado.query.filter_by(username=username_nuevo).first()
        if existente:
            return jsonify({'status': 'error', 'mensaje': 'El nombre de usuario ya existe.'}), 400
        usuario_actual.username = username_nuevo

    if password_nueva:
        usuario_actual.password = generate_password_hash(password_nueva)

    db.session.commit()
    session.clear()
    return jsonify({
        'status': 'ok',
        'redirect': '/login?msg=updated',
        'mensaje': 'Credenciales actualizadas. Por seguridad, inicia sesión nuevamente.'
    })


def construir_estado_empleado(empleado_id):
    usuario_actual = Empleado.query.get(empleado_id)
    turnos = HorarioGenerado.query.filter_by(empleado_id=empleado_id).all()

    permisos_aprobados = Solicitud.query.filter(
        Solicitud.empleado_id == empleado_id,
        Solicitud.estado.in_(['Aprobada', 'Modificada (Aprobada)'])
    ).all()

    mi_horario = {}
    for t in turnos:
        mi_horario[t.dia] = t.farmacia.nombre

    # Marcar los días de permisos o suspensiones en el horario
    for perm in permisos_aprobados:
        if not solicitud_cuenta_como_ausencia_ia(perm):
            continue
        try:
            dt = datetime.strptime(perm.fecha, '%Y-%m-%d')
            dia_semana = dt.weekday()

            # Solo sobrescribimos si no le tocó turno en otra farmacia ese mismo día
            if dia_semana not in mi_horario or mi_horario[dia_semana] == 'Descanso':
                if es_registro_administrativo_msg(perm.mensaje_admin):
                    mi_horario[dia_semana] = 'Suspensión'
                elif perm.tipo_permiso == 'parcial' and perm.hora_retorno:
                    mi_horario[dia_semana] = f"Permiso parcial hasta {perm.hora_retorno}"
                else:
                    mi_horario[dia_semana] = 'Permiso Aprobado'
        except Exception as e:
            print("Error al procesar permiso en vista empleado:", e)

    mis_solicitudes = Solicitud.query.filter_by(empleado_id=empleado_id).order_by(Solicitud.id.desc()).all()

    # Calcular las fechas de la semana actual (Lunes a Domingo)
    hoy = datetime.now()
    inicio_semana = hoy - timedelta(days=hoy.weekday())

    fechas_semana = []
    for i in range(7):
        fecha_dia = inicio_semana + timedelta(days=i)
        fechas_semana.append(fecha_dia.strftime('%d/%m/%Y'))

    return {
        'user': usuario_actual,
        'mi_horario': mi_horario,
        'solicitudes': mis_solicitudes,
        'fechas_semana': fechas_semana
    }


@app.route('/empleado/estado')
def empleado_estado():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return jsonify({'error': 'unauthorized'}), 401

    empleado_id = session['user_id']
    data = construir_estado_empleado(empleado_id)

    solicitudes_payload = []
    for s in data['solicitudes']:
        solicitudes_payload.append({
            'id': s.id,
            'fecha': s.fecha,
            'motivo': s.motivo,
            'estado': s.estado,
            'mensaje_admin': s.mensaje_admin or ''
        })

    return jsonify({
        'mi_horario': {str(k): v for k, v in data['mi_horario'].items()},
        'solicitudes': solicitudes_payload,
        'fechas_semana': data['fechas_semana']
    })


@app.route('/empleado/horario/json')
def empleado_horario_json():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    empleado_id = session['user_id']
    usuario = Empleado.query.get(empleado_id)
    turnos = HorarioGenerado.query.filter_by(empleado_id=empleado_id).all()

    aprobadas = Solicitud.query.filter(
        Solicitud.empleado_id == empleado_id,
        Solicitud.estado.in_(['Aprobada', 'Modificada (Aprobada)'])
    ).all()
    aprobadas = [s for s in aprobadas if solicitud_cuenta_como_ausencia_ia(s)]
    aprobadas_por_fecha = {s.fecha: s for s in aprobadas}

    turnos_por_dia = {t.dia: t for t in turnos}

    hoy = datetime.now()
    inicio_semana = hoy - timedelta(days=hoy.weekday())
    dias_nombre = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']

    resultado = []
    for i in range(7):
        fecha_dt = inicio_semana + timedelta(days=i)
        fecha_iso = fecha_dt.strftime('%Y-%m-%d')
        horario_texto = usuario.horario if usuario and usuario.horario else '8 AM - 6:00 PM'

        if fecha_iso in aprobadas_por_fecha:
            permiso = aprobadas_por_fecha[fecha_iso]
            if es_registro_administrativo_msg(permiso.mensaje_admin):
                resultado.append({
                    'dia': dias_nombre[i],
                    'fecha': fecha_iso,
                    'sucursal': 'Suspensión',
                    'horario': '-'
                })
            elif permiso.tipo_permiso == 'parcial' and permiso.hora_retorno:
                resultado.append({
                    'dia': dias_nombre[i],
                    'fecha': fecha_iso,
                    'sucursal': 'Permiso parcial',
                    'horario': f"Entra {permiso.hora_retorno}"
                })
            else:
                resultado.append({
                    'dia': dias_nombre[i],
                    'fecha': fecha_iso,
                    'sucursal': 'Permiso Aprobado',
                    'horario': '-'
                })
            continue

        turno = turnos_por_dia.get(i)
        if turno and turno.farmacia:
            resultado.append({
                'dia': dias_nombre[i],
                'fecha': fecha_iso,
                'sucursal': turno.farmacia.nombre,
                'horario': horario_texto
            })
        else:
            resultado.append({
                'dia': dias_nombre[i],
                'fecha': fecha_iso,
                'sucursal': '-',
                'horario': 'Descanso'
            })

    return jsonify(resultado)

@app.route('/logout')
def logout():
    empleado_id = session.get('user_id')
    if empleado_id:
        empleado = Empleado.query.get(empleado_id)
        if empleado:
            empleado.activo_ahora = False
            db.session.commit()
    session.clear()
    return redirect(url_for('login'))


@app.route('/dev/panel')
def dev_panel():
    if not acceso_dev():
        flash('Acceso no autorizado', 'error')
        return redirect('/')

    empleados = Empleado.query.all()
    return render_template('dev_panel.html', empleados=empleados)


@app.route('/dev/usuarios/json')
def dev_usuarios_json():
    if not acceso_dev():
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    empleados = Empleado.query.all()
    payload = []
    for e in empleados:
        sucursal = e.farmacia.nombre if e.farmacia else '--'
        ultimo_login = e.ultimo_login.strftime('%Y-%m-%d %H:%M') if e.ultimo_login else ''
        pwd = e.password or ''
        pwd_trunc = f"{pwd[:20]}..." if len(pwd) > 20 else pwd
        payload.append({
            'id': e.id,
            'nombre': e.nombre,
            'username': e.username,
            'password_hash': pwd_trunc,
            'rol': e.rol,
            'sucursal': sucursal,
            'ultimo_login': ultimo_login,
            'activo_ahora': bool(e.activo_ahora),
            'forzar_logout': bool(e.forzar_logout)
        })

    return jsonify(payload)


@app.route('/dev/reset-password/<int:id>', methods=['POST'])
def dev_reset_password(id):
    if not acceso_dev():
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    nueva_password = data.get('nueva_password', '').strip()
    if not nueva_password:
        return jsonify({'status': 'error', 'mensaje': 'Debes ingresar una nueva contraseña.'}), 400

    empleado = Empleado.query.get(id)
    if not empleado:
        return jsonify({'status': 'error', 'mensaje': 'Usuario no encontrado'}), 404

    empleado.password = generate_password_hash(nueva_password)
    db.session.commit()
    return jsonify({'status': 'ok', 'mensaje': f'Contraseña actualizada para {empleado.nombre}'})


@app.route('/dev/forzar-logout/<int:id>', methods=['POST'])
def dev_forzar_logout(id):
    if not acceso_dev():
        return jsonify({"status": "error", "mensaje": "No autorizado"}), 403

    empleado = Empleado.query.get_or_404(id)

    empleado.forzar_logout = True
    empleado.activo_ahora = False
    db.session.commit()
    return jsonify({
        "status": "ok",
        "mensaje": f"Sesión de {empleado.nombre} será cerrada en su próxima acción"
    })


@app.route('/dev/crear-admin', methods=['GET', 'POST'])
def dev_crear_admin():
    if session.get('rol') != 'Desarrollador':
        return jsonify({"status": "error", "mensaje": "No autorizado"}), 403

    if request.method == 'GET':
        empleados = Empleado.query.all()
        return render_template('dev_panel.html', empleados=empleados)

    data = request.get_json(silent=True) or {}
    nombre = data.get('nombre', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not nombre or not username or not password:
        return jsonify({"status": "error", "mensaje": "Todos los campos son obligatorios"}), 400

    if Empleado.query.filter_by(username=username).first():
        return jsonify({"status": "error", "mensaje": "El usuario ya existe"}), 400

    nuevo_admin = Empleado(
        nombre=nombre,
        rol='Admin',
        horario='SE AJUSTA A LA NECESIDAD',
        farmacia_id=None,
        username=username,
        password=generate_password_hash(password)
    )
    if hasattr(nuevo_admin, 'horario_variable'):
        nuevo_admin.horario_variable = True

    db.session.add(nuevo_admin)
    db.session.commit()
    return jsonify({"status": "ok", "mensaje": f"Admin {nombre} creado exitosamente"})

# --- CRUD DE EMPLEADOS ---
@app.route('/admin/empleado/nuevo', methods=['GET', 'POST'])
def nuevo_empleado():
    if 'user_id' not in session or session.get('rol') not in ('Admin', 'Desarrollador'):
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        rol_nuevo = request.form.get('rol', '')
        if rol_nuevo == 'Administrador':
            rol_nuevo = 'Admin'

        if rol_nuevo in ('Admin', 'Desarrollador') and session.get('rol') != 'Desarrollador':
            flash('Solo el desarrollador puede crear administradores')
            return redirect(url_for('nuevo_empleado'))

        fid = request.form.get('farmacia_id', '')
        
        # Procesar el horario a partir de los inputs de hora
        if 'es_comodin' in request.form:
            horario_final = "SE AJUSTA A LA NECESIDAD"
        else:
            inicio = request.form.get('hora_inicio', '')
            fin = request.form.get('hora_fin', '')
            horario_final = f"{inicio} - {fin}" if inicio and fin else "N/A"

        # Capturar el día fijo
        dia_fijo = request.form.get('dia_descanso_fijo')
        
        nuevo = Empleado(
            nombre=request.form['nombre'],
            rol=rol_nuevo,
            horario=horario_final,
            farmacia_id=None if fid == "" else int(fid),
            username=request.form['username'],
            password=request.form['password'],
            dia_descanso_fijo=int(dia_fijo) if dia_fijo else None
        )
        
        db.session.add(nuevo)
        db.session.commit()
        return redirect(url_for('admin_dashboard'))
        
    farmacias = Farmacia.query.all()
    return render_template('empleado_form.html', farmacias=farmacias, empleado=None)

@app.route('/admin/empleado/editar/<int:id>', methods=['GET', 'POST'])
def editar_empleado(id):
    if 'user_id' not in session or session.get('rol') not in ('Admin', 'Desarrollador'):
        return redirect(url_for('login'))
        
    empleado = Empleado.query.get(id)

    def normalizar_hora(valor):
        if not valor:
            return ''
        if hasattr(valor, 'strftime'):
            return valor.strftime('%H:%M')
        return str(valor)[:5] if isinstance(valor, str) and len(valor) >= 5 else str(valor)

    if request.method == 'POST':
        empleado.nombre = request.form.get('nombre', empleado.nombre)
        rol_nuevo = request.form.get('rol', empleado.rol)
        if rol_nuevo == 'Administrador':
            rol_nuevo = 'Admin'
        if rol_nuevo in ('Admin', 'Desarrollador') and session.get('rol') != 'Desarrollador':
            flash('Solo el desarrollador puede crear administradores')
            return redirect(url_for('editar_empleado', id=id))
        empleado.rol = rol_nuevo

        horario_variable = 'horario_variable' in request.form or 'es_comodin' in request.form
        hora_entrada = request.form.get('hora_entrada', request.form.get('hora_inicio', '')).strip()
        hora_salida = request.form.get('hora_salida', request.form.get('hora_fin', '')).strip()

        if horario_variable:
            empleado.horario = "SE AJUSTA A LA NECESIDAD"
        else:
            if not hora_entrada or not hora_salida:
                farmacias = Farmacia.query.all()
                return render_template(
                    'empleado_form.html',
                    farmacias=farmacias,
                    empleado=empleado,
                    hora_entrada_str=hora_entrada,
                    hora_salida_str=hora_salida,
                    horario_variable=horario_variable,
                    error_horario='Por favor ingresa el horario base del empleado'
                )
            empleado.horario = f"{hora_entrada} - {hora_salida}"

        # Capturar y asignar el día fijo
        dia_fijo = request.form.get('dia_descanso_fijo')
        empleado.dia_descanso_fijo = int(dia_fijo) if dia_fijo else None

        fid = request.form.get('farmacia_id', '')
        empleado.farmacia_id = None if fid == "" else int(fid)
        empleado.username = request.form.get('username', empleado.username)
        
        if request.form['password']: # Solo actualiza clave si no está en blanco
            empleado.password = request.form['password']
            
        db.session.commit()
        return redirect(url_for('admin_dashboard'))
        
    farmacias = Farmacia.query.all()
    hora_entrada_str = ''
    hora_salida_str = ''
    horario_variable = False
    if empleado and empleado.horario and ' - ' in empleado.horario:
        partes = empleado.horario.split(' - ')
        if len(partes) == 2:
            hora_entrada_str = normalizar_hora(partes[0])
            hora_salida_str = normalizar_hora(partes[1])
    elif empleado:
        hora_entrada_str = normalizar_hora(getattr(empleado, 'hora_entrada', ''))
        hora_salida_str = normalizar_hora(getattr(empleado, 'hora_salida', ''))

    if empleado:
        horario_variable = bool(getattr(empleado, 'horario_variable', False)) or empleado.horario == 'SE AJUSTA A LA NECESIDAD'

    return render_template('empleado_form.html', farmacias=farmacias, empleado=empleado,
                           hora_entrada_str=hora_entrada_str,
                           hora_salida_str=hora_salida_str,
                           horario_variable=horario_variable)

@app.route('/admin/empleado/eliminar/<int:id>')
def eliminar_empleado(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    empleado = Empleado.query.get(id)
    if empleado:
        try:
            Solicitud.query.filter_by(cobertura_empleado_id=id).update(
                {Solicitud.cobertura_empleado_id: None},
                synchronize_session=False
            )
            Solicitud.query.filter_by(empleado_id=id).delete(synchronize_session=False)
            HorarioGenerado.query.filter_by(empleado_id=id).delete(synchronize_session=False)
            AsignacionTemporal.query.filter_by(empleado_id=id).delete(synchronize_session=False)

            db.session.delete(empleado)
            db.session.commit()
            flash('Empleado eliminado correctamente.')
        except Exception:
            db.session.rollback()
            flash('No se pudo eliminar el empleado porque tiene registros relacionados.', 'error')
    return redirect(url_for('admin_dashboard'))

# --- CRUD DE FARMACIAS ---
@app.route('/admin/farmacias')
def admin_farmacias():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    farmacias = Farmacia.query.all()
    return render_template('farmacias.html', farmacias=farmacias, nombre=session['nombre'])

@app.route('/admin/farmacia/nueva', methods=['GET', 'POST'])
def nueva_farmacia():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    if request.method == 'POST':
        inicio = request.form['hora_inicio']
        fin = request.form['hora_fin']
        jornada_final = f"{inicio} - {fin}" if inicio and fin else "N/A"
        
        nueva = Farmacia(
            nombre=request.form['nombre'],
            jornada=jornada_final
        )
        db.session.add(nueva)
        db.session.commit()
        return redirect(url_for('admin_farmacias'))
        
    return render_template('farmacia_form.html', farmacia=None)

@app.route('/admin/farmacia/editar/<int:id>', methods=['GET', 'POST'])
def editar_farmacia(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    farmacia = Farmacia.query.get(id)
    if request.method == 'POST':
        farmacia.nombre = request.form['nombre']
        inicio = request.form.get('hora_inicio', '')
        fin = request.form.get('hora_fin', '')
        if inicio and fin:
            farmacia.jornada = f"{inicio} - {fin}"
            
        db.session.commit()
        return redirect(url_for('admin_farmacias'))
        
    return render_template('farmacia_form.html', farmacia=farmacia)

@app.route('/admin/farmacia/eliminar/<int:id>')
def eliminar_farmacia(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    farmacia = Farmacia.query.get(id)
    if farmacia:
        # Poner en None a los empleados que estaban en esta farmacia
        for emp in farmacia.empleados:
            emp.farmacia_id = None
        db.session.delete(farmacia)
        db.session.commit()
    return redirect(url_for('admin_farmacias'))

# --- MOTOR DE INTELIGENCIA ARTIFICIAL ---
@app.route('/admin/generar_ia')
def ejecutar_ia():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))

    try:
        empleados_db = Empleado.query.filter(Empleado.rol != 'Admin').all()
        farmacias_db = Farmacia.query.all()

        # AUSENCIAS APROBADAS
        aprobadas = Solicitud.query.filter(
            Solicitud.estado.in_(['Aprobada', 'Modificada (Aprobada)'])
        ).all()

        permisos_por_empleado = {}
        for s in aprobadas:
            if not solicitud_cuenta_como_ausencia_ia(s):
                continue
            try:
                dt = datetime.strptime(s.fecha, '%Y-%m-%d')
                permisos_por_empleado.setdefault(s.empleado_id, set()).add(dt.weekday())
            except Exception:
                continue

        # Respeta descanso rotativo previo si ya existe horario generado
        # Cumplimiento Art. 126 Código de Trabajo Guatemala - Descanso semanal obligatorio
        descanso_rotativo = {}
        for e in empleados_db:
            if e.dia_descanso_fijo is not None:
                continue
            turnos_actuales = HorarioGenerado.query.filter_by(empleado_id=e.id).all()
            if not turnos_actuales:
                continue
            dias_trabajados = {t.dia for t in turnos_actuales}
            dias_permiso = permisos_por_empleado.get(e.id, set())
            for d in range(7):
                if d not in dias_trabajados and d not in dias_permiso:
                    descanso_rotativo[e.id] = d
                    break

        empleados_data = []
        for e in Empleado.query.filter(
            Empleado.rol.in_(['Dependiente', 'Comodin']),
            Empleado.rol != 'Administrador',
            Empleado.rol != 'Desarrollador'
        ).all():
            # Verificar que tenga los datos mínimos necesarios
            horario_variable = bool(getattr(e, 'horario_variable', False)) or e.horario == 'SE AJUSTA A LA NECESIDAD'
            hora_entrada = getattr(e, 'hora_entrada', None)
            hora_salida = getattr(e, 'hora_salida', None)
            if (hora_entrada is None or hora_salida is None) and e.horario and ' - ' in e.horario:
                partes = e.horario.split(' - ')
                if len(partes) == 2:
                    hora_entrada = partes[0].strip()
                    hora_salida = partes[1].strip()

            if not horario_variable and (not hora_entrada or not hora_salida):
                continue  # Saltar empleados sin horario definido

            dia_descanso = getattr(e, 'dia_descanso', None)
            if dia_descanso in [None, 'Ninguno', 'Rotativo', 'ninguno', '']:
                dia_descanso = e.dia_descanso_fijo
                if dia_descanso is None and e.id in descanso_rotativo:
                    dia_descanso = descanso_rotativo[e.id]

            empleados_data.append({
                'id': e.id,
                'nombre': e.nombre,           # ← clave exacta que usa OR-Tools
                'rol': e.rol,
                'farmacia_id': e.farmacia_id,
                'horario_variable': horario_variable,
                'dia_descanso_fijo': dia_descanso
            })

        ausencias_lista = []
        for s in aprobadas:
            if not solicitud_cuenta_como_ausencia_ia(s):
                continue
            try:
                dt = datetime.strptime(s.fecha, '%Y-%m-%d')
                empleado_ausente = Empleado.query.get(s.empleado_id)

                ausencias_lista.append({
                    'empleado_id': s.empleado_id,
                    'dia': dt.weekday(),
                    'farmacia_id': empleado_ausente.farmacia_id if empleado_ausente else None
                })
            except Exception as e:
                print("Error procesando ausencia:", e)

        # ASIGNACIONES FORZADAS
        asignaciones_forzadas = []
        try:
            asignaciones_db = AsignacionTemporal.query.all()
            for a in asignaciones_db:
                try:
                    dt = datetime.strptime(a.fecha, '%Y-%m-%d')
                    asignaciones_forzadas.append({
                        'empleado_id': a.empleado_id,
                        'farmacia_dest_id': a.farmacia_destino_id,
                        'dia': dt.weekday()
                    })
                except Exception as e:
                    print("Error procesando asignación forzada:", e)
        except Exception as e:
            print("Aviso: No se pudieron procesar las asignaciones forzadas:", e)

        farmacias_data = []
        for f in Farmacia.query.all():
            farmacias_data.append({
                'id': f.id,
                'nombre': f.nombre      # ← clave exacta
            })

        try:
            resultados = generar_horario_semana(
                empleados_data,
                farmacias_data,
                ausencias=ausencias_lista,
                forzadas=asignaciones_forzadas
            )
        except KeyError as e:
            print(f"[ERROR KeyError] Clave faltante en diccionario: {e}")
            print(f"[ERROR] Primer empleado recibido: {empleados_data[0] if empleados_data else 'LISTA VACÍA'}")
            flash(
                "Error al optimizar: datos de permisos o asignaciones no coinciden con los empleados "
                f"incluidos en el motor (referencia {e}). Revisa empleados sin horario completo o sucursales eliminadas.",
                "error",
            )
            return redirect(url_for("ver_horarios_ia"))
        except Exception as e:
            print(f"[ERROR General] {type(e).__name__}: {e}")
            flash(f"Error interno al ejecutar el motor: {str(e)}", "error")
            return redirect(url_for("ver_horarios_ia"))

        if resultados is not None:
            HorarioGenerado.query.delete()

            for r in resultados:
                nuevo_turno = HorarioGenerado(
                    dia=r['dia'],
                    empleado_id=r['empleado_id'],
                    farmacia_id=r['farmacia_id']
                )
                db.session.add(nuevo_turno)

            db.session.commit()
            flash('¡Horarios optimizados generados exitosamente!', 'success')
        else:
            flash('Error: OR-Tools no encontró una solución válida. Revisa restricciones en conflicto.', 'error')

    except Exception as e:
        print("ERROR GENERAL EN ejecutar_ia:", e)
        flash(f'Error interno al ejecutar la IA: {str(e)}', 'error')

    return redirect(url_for('ver_horarios_ia'))

@app.route('/admin/horarios_ia')
def ver_horarios_ia():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    horarios_generados = HorarioGenerado.query.order_by(HorarioGenerado.empleado_id, HorarioGenerado.dia).all()
    permisos_aprobados = Solicitud.query.filter(Solicitud.estado.in_(['Aprobada', 'Modificada (Aprobada)'])).all()
    
    empleados_agrupados = {}
    
    # Agregar primero a todos los empleados que tienen horarios generados
    for turno in horarios_generados:
        emp_nombre = turno.empleado.nombre
        if emp_nombre not in empleados_agrupados:
            empleados_agrupados[emp_nombre] = {
                'id': turno.empleado.id,          
                'rol': turno.empleado.rol,
                'dias': {}
            }
        empleados_agrupados[emp_nombre]['dias'][turno.dia] = turno.farmacia.nombre

    # Marcar los días de permisos aprobados
    for perm in permisos_aprobados:
        if not solicitud_cuenta_como_ausencia_ia(perm):
            continue
        try:
            dt = datetime.strptime(perm.fecha, '%Y-%m-%d')
            dia_semana = dt.weekday()
            
            emp = Empleado.query.get(perm.empleado_id)
            if emp:
                if emp.nombre not in empleados_agrupados:
                    empleados_agrupados[emp.nombre] = {
                        'id': emp.id,             
                        'rol': emp.rol,
                        'dias': {}
                    }
                # Solo sobrescribe si no le asignaron turno
                if dia_semana not in empleados_agrupados[emp.nombre]['dias']:
                    if es_registro_administrativo_msg(perm.mensaje_admin):
                        empleados_agrupados[emp.nombre]['dias'][dia_semana] = 'Suspensión'
                    else:
                        empleados_agrupados[emp.nombre]['dias'][dia_semana] = 'Permiso Aprobado'
        except Exception as e:
            print("Error procesando fecha de permiso en vista:", e)

    return render_template('horarios_ia.html', 
                           empleados_agrupados=empleados_agrupados, 
                           nombre=session.get('nombre'))

# --- RUTAS DE LIMPIEZA DE HORARIOS ---
@app.route('/admin/horarios/limpiar_todo', methods=['POST'])
def limpiar_horarios_todo():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    try:
        # 1. Borrar todos los horarios generados
        HorarioGenerado.query.delete()
        # 2. Borrar todas las asignaciones manuales/forzadas
        AsignacionTemporal.query.delete()
        db.session.commit()
        flash('Todos los horarios y asignaciones manuales han sido limpiados.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al limpiar: {str(e)}', 'error')
        
    return redirect(url_for('ver_horarios_ia'))

@app.route('/admin/horarios/limpiar_empleado/<int:empleado_id>', methods=['POST'])
def limpiar_horario_empleado(empleado_id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    try:
        # Borrar el horario específico de este empleado
        HorarioGenerado.query.filter_by(empleado_id=empleado_id).delete()
        
        # Borrar sus asignaciones temporales
        AsignacionTemporal.query.filter_by(empleado_id=empleado_id).delete()
        
        db.session.commit()
        flash('El horario y asignaciones de este empleado han sido eliminados.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error al limpiar empleado: {str(e)}', 'error')
        
    return redirect(url_for('ver_horarios_ia'))

# --- ASIGNACIÓN TEMPORAL ---
@app.route('/admin/asignar_temporal/<int:emp_id>', methods=['GET', 'POST'])
def asignar_temporal(emp_id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    empleado = Empleado.query.get(emp_id)
    farmacias = Farmacia.query.all()
    
    if request.method == 'POST':
        farmacia_dest_id = request.form['farmacia_id']
        fecha = request.form['fecha']
        
        # Guardar la orden
        nueva_asig = AsignacionTemporal(empleado_id=emp_id, farmacia_destino_id=farmacia_dest_id, fecha=fecha)
        db.session.add(nueva_asig)
        db.session.commit()
        
        flash(f'¡Asignación forzada guardada! La IA enviará a {empleado.nombre} a esa sucursal en el próximo cálculo.', 'success')
        return redirect(url_for('admin_dashboard'))
        
    return render_template('asignacion_form.html', empleado=empleado, farmacias=farmacias)

# --- CANCELACIÓN DE PERMISOS ---
@app.route('/empleado/solicitud/cancelar/<int:id>', methods=['POST'])
def solicitar_cancelacion(id):
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    
    # Bloquear intento de cancelar suspensiones
    if solicitud and es_registro_administrativo_msg(solicitud.mensaje_admin):
        flash('No tienes permiso para cancelar registros administrativos.', 'error')
        return redirect(url_for('empleado_permisos'))
        
    if solicitud and solicitud.empleado_id == session['user_id']:
        nota = (request.form.get('nota_empleado') or '').strip()[:300]
        if nota:
            solicitud.nota_empleado = nota
        solicitud.estado_al_pedir_cancel = solicitud.estado
        solicitud.estado = 'Pide Cancelación'
        db.session.commit()
        flash('Solicitud de cancelación enviada al administrador.', 'success')
        
    return redirect(url_for('empleado_permisos'))


@app.route('/empleado/solicitud/impugnar', methods=['POST'])
def empleado_impugnar_consecuencia():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return redirect(url_for('login'))

    empleado_id = session['user_id']
    sid = request.form.get('suspension_solicitud_id', type=int)
    motivo = (request.form.get('motivo') or '').strip()

    susp = Solicitud.query.get(sid)
    if not susp or susp.empleado_id != empleado_id:
        flash('Solicitud no válida.', 'error')
        return redirect(url_for('empleado_permisos'))

    if not es_registro_administrativo_msg(susp.mensaje_admin):
        flash('Solo puedes impugnar sanciones registradas por administración.', 'error')
        return redirect(url_for('empleado_permisos'))

    if susp.estado not in ('Aprobada', 'Modificada (Aprobada)'):
        flash('Esta sanción ya no admite impugnación en este estado.', 'error')
        return redirect(url_for('empleado_permisos'))

    if len(motivo) < 5:
        flash('Describe el motivo de la impugnación (al menos 5 caracteres).', 'error')
        return redirect(url_for('empleado_permisos'))

    for p in Solicitud.query.filter_by(empleado_id=empleado_id, fecha=susp.fecha, estado='Pendiente').all():
        if categoria_solicitud(p) == 'cancelacion_falta':
            flash('Ya tienes una impugnación pendiente para esa fecha.', 'error')
            return redirect(url_for('empleado_permisos'))

    nueva = Solicitud(
        empleado_id=empleado_id,
        fecha=susp.fecha,
        motivo=motivo,
        estado='Pendiente',
        mensaje_admin='',
        tipo_permiso='dia_completo',
        hora_retorno=None,
        categoria='cancelacion_falta',
    )
    db.session.add(nueva)
    db.session.commit()
    flash('Impugnación enviada. El administrador la revisará.', 'success')
    return redirect(url_for('empleado_permisos'))


@app.route('/empleado/solicitud/nueva', methods=['POST'])
def nueva_solicitud_empleado():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    fecha = request.form.get('fecha')
    motivo = (request.form.get('motivo') or '').strip()
    tipo_permiso = request.form.get('tipo_permiso', 'dia_completo')
    hora_retorno = request.form.get('hora_retorno', '').strip()
    categoria = (request.form.get('categoria') or 'permiso').strip()
    if categoria not in ('permiso', 'cambio_descanso'):
        categoria = 'permiso'

    if categoria == 'permiso':
        if not fecha or not motivo:
            return jsonify({'status': 'error', 'mensaje': 'Debes completar la fecha y el motivo.'}), 400
        if tipo_permiso == 'parcial' and not hora_retorno:
            return jsonify({'status': 'error', 'mensaje': 'Debes indicar la hora de entrada.'}), 400
        nueva = Solicitud(
            empleado_id=session['user_id'],
            fecha=fecha,
            motivo=motivo,
            estado='Pendiente',
            mensaje_admin='',
            tipo_permiso=tipo_permiso,
            hora_retorno=hora_retorno if tipo_permiso == 'parcial' else None,
            categoria='permiso',
        )
        db.session.add(nueva)
        db.session.commit()
        return jsonify({'status': 'ok'})

    if categoria == 'cambio_descanso':
        motivo_cd = motivo or 'Cambio de día de descanso'
        try:
            d = int(request.form.get('dia_descanso_solicitado', ''))
        except (TypeError, ValueError):
            d = -1
        if d < 0 or d > 6:
            return jsonify({'status': 'error', 'mensaje': 'Selecciona un día de descanso válido.'}), 400
        if not fecha:
            return jsonify({'status': 'error', 'mensaje': 'Indica una fecha de referencia (semana objetivo).'}), 400
        nueva = Solicitud(
            empleado_id=session['user_id'],
            fecha=fecha,
            motivo=motivo_cd,
            estado='Pendiente',
            mensaje_admin='',
            tipo_permiso='dia_completo',
            hora_retorno=None,
            categoria='cambio_descanso',
            dia_descanso_solicitado=d,
        )
        db.session.add(nueva)
        db.session.commit()
        return jsonify({'status': 'ok'})

    if categoria == 'cancelacion_falta':
        return jsonify({
            'status': 'error',
            'mensaje': 'Para impugnar una sanción administrativa usa el botón «Impugnar» en la tabla Mis solicitudes.',
        }), 400

    return jsonify({'status': 'error', 'mensaje': 'Tipo de solicitud no reconocido.'}), 400


@app.route('/empleado/solicitudes/json')
def empleado_solicitudes_json():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    empleado_id = session['user_id']
    solicitudes = Solicitud.query.filter_by(empleado_id=empleado_id).order_by(Solicitud.id.desc()).all()
    payload = []
    for s in solicitudes:
        payload.append({
            'id': s.id,
            'fecha': s.fecha,
            'motivo': s.motivo,
            'estado': s.estado,
            'mensaje_admin': s.mensaje_admin or '',
            'tipo_permiso': s.tipo_permiso or 'dia_completo',
            'hora_retorno': s.hora_retorno or '',
            'categoria': categoria_solicitud(s),
            'dia_descanso_solicitado': s.dia_descanso_solicitado,
            'nota_empleado': (s.nota_empleado or ''),
        })

    return jsonify({'status': 'ok', 'solicitudes': payload})

@app.route('/admin/solicitudes/confirmar_cancelacion/<int:id>', methods=['GET', 'POST'])
def confirmar_cancelacion(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    if solicitud and solicitud.estado == 'Pide Cancelación':
        solicitud.estado = 'Cancelada'
        solicitud.estado_al_pedir_cancel = None

        dia_semana = None
        try:
            dia_semana = datetime.strptime(solicitud.fecha, '%Y-%m-%d').weekday()
        except Exception:
            dia_semana = None

        empleado = Empleado.query.get(solicitud.empleado_id)
        if empleado and dia_semana is not None:
            cob_id = solicitud.cobertura_empleado_id
            if cob_id:
                HorarioGenerado.query.filter_by(empleado_id=cob_id, dia=dia_semana).delete()
                AsignacionTemporal.query.filter_by(empleado_id=cob_id, fecha=solicitud.fecha).delete()

            if empleado.farmacia_id is not None:
                HorarioGenerado.query.filter_by(empleado_id=empleado.id, dia=dia_semana).delete()
                AsignacionTemporal.query.filter_by(empleado_id=empleado.id, fecha=solicitud.fecha).delete()

                if empleado.dia_descanso_fijo is None or empleado.dia_descanso_fijo != dia_semana:
                    turno_restaurado = HorarioGenerado.query.filter_by(empleado_id=empleado.id, dia=dia_semana).first()
                    if turno_restaurado:
                        turno_restaurado.farmacia_id = empleado.farmacia_id
                    else:
                        turno_restaurado = HorarioGenerado(
                            dia=dia_semana,
                            empleado_id=empleado.id,
                            farmacia_id=empleado.farmacia_id
                        )
                        db.session.add(turno_restaurado)

        db.session.commit()
        flash('Cancelación aceptada. Revisa horarios y ejecuta OR-Tools si aplica.', 'success')
        return redirect(url_for('admin_solicitudes'))

    return redirect(url_for('admin_solicitudes'))


@app.route('/admin/solicitudes/rechazar_cancelacion/<int:id>', methods=['GET', 'POST'])
def rechazar_cancelacion_solicitud(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))

    solicitud = Solicitud.query.get(id)
    if solicitud and solicitud.estado == 'Pide Cancelación':
        prev = solicitud.estado_al_pedir_cancel or 'Aprobada'
        solicitud.estado = prev
        solicitud.estado_al_pedir_cancel = None
        base = (solicitud.mensaje_admin or '').strip()
        suf = 'Cancelación rechazada por administración.'
        solicitud.mensaje_admin = (base + (' · ' if base else '') + suf)[:200]
        db.session.commit()
        flash('Se rechazó la cancelación; el permiso conserva su estado anterior.', 'info')

    return redirect(url_for('admin_solicitudes'))

# --- INICIALIZACIÓN Y CARGA DE DATOS (SEED) ---
def seed_data():
    # Si ya hay farmacias, no insertamos de nuevo
    if Farmacia.query.first():
        return

    # 1. Crear Farmacias
    farmacias_data = [
        (1, 'Amatitlan', '8 AM - 6:30 PM'),
        (2, 'Loren', '8 AM - 6:00 PM'),
        (3, 'San José Pila', '8 AM - 6:00 PM'),
        (4, 'San josé Parque', '8 AM - 6:00 PM'),
        (5, 'Venecia', '7:30 AM - 5:00 PM'),
        (6, 'Alioto', '8 AM - 7:00 PM'),
        (7, 'Alioto 2', '6 AM - 6:00 PM'),
        (8, 'Primavera', '8 AM - 6:00 PM'),
        (9, 'San Miguel', '8 AM - 6:00 PM'),
        (10, 'Prados', '10 AM - 9 PM'),
        (11, 'Linda Vista 10ma', '8 AM - 8 PM'),
        (12, 'Linda Vista 8va', '8 AM - 6:00 PM'),
        (13, 'Linda Vista 13', '8 AM - 6:00 PM')
    ]
    for fid, nom, jor in farmacias_data:
        db.session.add(Farmacia(id=fid, nombre=nom, jornada=jor))

    # 2. Crear Empleados (dia_descanso_fijo quedará en NULL por defecto)
    empleados_data = [
        ('Rubi', 'Dependiente', '8 AM - 6:00 PM', 4),
        ('Mishel', 'Dependiente', '8 AM - 6:00 PM', 3),
        ('Xiomara', 'Dependiente', '8 AM - 4:00 PM', 1),
        ('Jaqueline', 'Dependiente', '8 AM - 6:00 PM', 4),
        ('Sarai', 'Comodin', 'SE AJUSTA A LA NECESIDAD', None),
        ('Jocsabed', 'Comodin', 'SE AJUSTA A LA NECESIDAD', None),
        ('Gaby', 'Dependiente', '10 AM - 6:30 PM', 1),
        ('Ana', 'Dependiente', '7:30 AM - 5:00 PM', 5),
        ('Karlota', 'Dependiente', '10 AM - 7:00 PM', 6),
        ('Daisy', 'Dependiente', '8 AM - 5:00 PM', 6),
        ('Maribel', 'Dependiente', '10 AM - 6:00 PM', 7),
        ('Esther', 'Dependiente', '10 AM - 9:00 PM', 10),
        ('Isabel', 'Dependiente', '8 AM - 6:00 PM', 9),
        ('Nehemias', 'Admin', 'N/A', None),
        ('Vilma', 'Dependiente', '8 AM - 6:00 PM', 2),
        ('Efrain', 'Dependiente', '8 AM - 6:00 PM', 12),
        ('Nueva', 'Dependiente', '8 AM - 8 PM', 11),
        ('Ada', 'Dependiente', '8 AM - 6:00 PM', 13),
        ('Susana', 'Admin', 'N/A', None)
    ]

    for nom, rol, hor, fid in empleados_data:
        usr = nom.lower() # usuario es su nombre en minusculas
        pwd = '123'       # contraseña generica para todos
        db.session.add(Empleado(nombre=nom, rol=rol, horario=hor, farmacia_id=fid, username=usr, password=pwd))

    db.session.commit()


def asegurar_columnas_empleado():
    inspector = inspect(db.engine)
    tablas = inspector.get_table_names()
    if 'empleado' not in tablas:
        return

    columnas = {col['name'] for col in inspector.get_columns('empleado')}
    sentencias = []

    if 'ultimo_login' not in columnas:
        sentencias.append('ALTER TABLE empleado ADD COLUMN ultimo_login TIMESTAMP')
    if 'activo_ahora' not in columnas:
        sentencias.append("ALTER TABLE empleado ADD COLUMN activo_ahora BOOLEAN DEFAULT false")
    if 'forzar_logout' not in columnas:
        sentencias.append("ALTER TABLE empleado ADD COLUMN forzar_logout BOOLEAN DEFAULT false")

    if not sentencias:
        return

    with db.engine.begin() as conn:
        for sentencia in sentencias:
            conn.execute(text(sentencia))


def asegurar_columnas_solicitud():
    inspector = inspect(db.engine)
    tablas = inspector.get_table_names()
    if 'solicitud' not in tablas:
        return

    columnas = {col['name'] for col in inspector.get_columns('solicitud')}
    sentencias = []

    if 'tipo_permiso' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN tipo_permiso VARCHAR(20) DEFAULT 'dia_completo'")
    if 'hora_retorno' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN hora_retorno VARCHAR(10)")
    if 'cobertura_empleado_id' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN cobertura_empleado_id INTEGER")
    if 'categoria' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN categoria VARCHAR(32) DEFAULT 'permiso'")
    if 'dia_descanso_solicitado' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN dia_descanso_solicitado INTEGER")
    if 'estado_al_pedir_cancel' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN estado_al_pedir_cancel VARCHAR(50)")
    if 'nota_empleado' not in columnas:
        sentencias.append("ALTER TABLE solicitud ADD COLUMN nota_empleado VARCHAR(300)")

    if not sentencias:
        return

    with db.engine.begin() as conn:
        for sentencia in sentencias:
            conn.execute(text(sentencia))

with app.app_context():
    db.create_all()
    asegurar_columnas_empleado()
    asegurar_columnas_solicitud()
    seed_data()

if __name__ == '__main__':
    app.run(debug=True, port=5000)