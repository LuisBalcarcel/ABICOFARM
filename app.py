from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
import os
from optimizer import generar_horario_semana 
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_key')

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    DATABASE_URL = 'sqlite:///instance/abicofarm.db'

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
    password = db.Column(db.String(50))
    
    # NUEVO: Día fijo de descanso (0=Lunes, 6=Domingo)
    dia_descanso_fijo = db.Column(db.Integer, nullable=True) 

    farmacia_id = db.Column(db.Integer, db.ForeignKey('farmacia.id'), nullable=True)
    farmacia = db.relationship('Farmacia', backref=db.backref('empleados', lazy=True))

class HorarioGenerado(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dia = db.Column(db.Integer) # 0=Lunes, 1=Martes... 5=Sábado
    empleado_id = db.Column(db.Integer, db.ForeignKey('empleado.id'))
    farmacia_id = db.Column(db.Integer, db.ForeignKey('farmacia.id'))
    
    empleado = db.relationship('Empleado')
    farmacia = db.relationship('Farmacia')

class Solicitud(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empleado_id = db.Column(db.Integer, db.ForeignKey('empleado.id'))
    fecha = db.Column(db.String(20)) # Guardará "YYYY-MM-DD"
    motivo = db.Column(db.String(200))
    estado = db.Column(db.String(50), default='Pendiente') # Pendiente, Aprobada, Rechazada, Modificada
    mensaje_admin = db.Column(db.String(200), default='') # Para la contraoferta
    
    empleado = db.relationship('Empleado')

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

        user = Empleado.query.filter_by(username=username, password=password).first()
        if user:
            session['user_id'] = user.id
            session['rol'] = user.rol
            session['nombre'] = user.nombre
            session.permanent = False
            if user.rol == 'Admin':
                return redirect(url_for('admin_dashboard'))
            else:
                return redirect(url_for('empleado_dashboard'))
        else:
            flash('Usuario o contraseña incorrectos', 'error')

    return render_template('login.html')

@app.route('/admin')
def admin_dashboard():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))

    empleados = Empleado.query.all()
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
            'mensaje_admin': s.mensaje_admin or ''
        })

    return jsonify({'solicitudes': payload})

# Ruta para que el Admin pueda cancelar directamente una suspensión
@app.route('/admin/solicitudes/forzar_cancelacion/<int:id>', methods=['POST'])
def admin_forzar_cancelacion(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    if solicitud and solicitud.mensaje_admin == 'Registro Administrativo Directo':
        solicitud.estado = 'Cancelada'
        db.session.commit()
        flash('Suspensión administrativa cancelada exitosamente. Se recomienda ejecutar el Motor de IA para actualizar.', 'success')

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'mensaje': 'Solicitud cancelada'})

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
    solicitud = Solicitud.query.get(id)
    if not solicitud or estado not in ['Aprobada', 'Rechazada']:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'mensaje': 'Solicitud no encontrada'}), 404
        return redirect(url_for('admin_solicitudes'))

    if estado == 'Rechazada':
        solicitud.estado = estado
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'ok', 'mensaje': 'Solicitud rechazada'})
        return redirect(url_for('admin_solicitudes'))

    solicitud.estado = estado

    try:
        dt = datetime.strptime(solicitud.fecha, '%Y-%m-%d')
        dia_semana = dt.weekday()
    except Exception:
        dia_semana = None

    turno = None
    farmacia_id = None
    if dia_semana is not None:
        turno = HorarioGenerado.query.filter_by(empleado_id=solicitud.empleado_id, dia=dia_semana).first()
        if turno:
            farmacia_id = turno.farmacia_id
            db.session.delete(turno)

    if farmacia_id is not None and dia_semana is not None:
        comodines = Empleado.query.filter_by(rol='Comodin').all()
        comodin_disponible = None
        for comodin in comodines:
            if comodin.dia_descanso_fijo is not None and comodin.dia_descanso_fijo == dia_semana:
                continue
            ocupado = HorarioGenerado.query.filter_by(empleado_id=comodin.id, dia=dia_semana).first()
            if not ocupado:
                comodin_disponible = comodin
                break

        if comodin_disponible:
            reemplazo = HorarioGenerado(
                dia=dia_semana,
                empleado_id=comodin_disponible.id,
                farmacia_id=farmacia_id
            )
            db.session.add(reemplazo)

    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'ok', 'mensaje': 'Solicitud aprobada y horario actualizado'})
    return redirect(url_for('admin_solicitudes'))

@app.route('/admin/solicitudes/modificar/<int:id>', methods=['GET', 'POST'])
def modificar_solicitud(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    if request.method == 'POST':
        solicitud.fecha = request.form['nueva_fecha']
        solicitud.mensaje_admin = request.form['mensaje']
        solicitud.estado = 'Modificada (Aprobada)' # Cuenta como aprobada pero con cambios
        db.session.commit()
        return redirect(url_for('admin_solicitudes'))
        
    return render_template('solicitud_modificar.html', solicitud=solicitud, nombre=session['nombre'])

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
        try:
            dt = datetime.strptime(perm.fecha, '%Y-%m-%d')
            dia_semana = dt.weekday()

            # Solo sobrescribimos si no le tocó turno en otra farmacia ese mismo día
            if dia_semana not in mi_horario or mi_horario[dia_semana] == 'Descanso':
                if perm.mensaje_admin == 'Registro Administrativo Directo':
                    mi_horario[dia_semana] = 'Suspensión'
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
            if permiso.mensaje_admin == 'Registro Administrativo Directo':
                resultado.append({
                    'dia': dias_nombre[i],
                    'fecha': fecha_iso,
                    'sucursal': 'Suspensión',
                    'horario': '-'
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
    session.clear()
    return redirect(url_for('login'))

# --- CRUD DE EMPLEADOS ---
@app.route('/admin/empleado/nuevo', methods=['GET', 'POST'])
def nuevo_empleado():
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        fid = request.form['farmacia_id']
        
        # Procesar el horario a partir de los inputs de hora
        if 'es_comodin' in request.form:
            horario_final = "SE AJUSTA A LA NECESIDAD"
        else:
            inicio = request.form['hora_inicio']
            fin = request.form['hora_fin']
            horario_final = f"{inicio} - {fin}" if inicio and fin else "N/A"

        # Capturar el día fijo
        dia_fijo = request.form.get('dia_descanso_fijo')
        
        nuevo = Empleado(
            nombre=request.form['nombre'],
            rol=request.form['rol'],
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
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    empleado = Empleado.query.get(id)
    if request.method == 'POST':
        empleado.nombre = request.form['nombre']
        empleado.rol = request.form['rol']
        
        # Procesar el horario a partir de los inputs de hora
        if 'es_comodin' in request.form:
            empleado.horario = "SE AJUSTA A LA NECESIDAD"
        else:
            inicio = request.form.get('hora_inicio', '')
            fin = request.form.get('hora_fin', '')
            if inicio and fin:
                empleado.horario = f"{inicio} - {fin}"

        # Capturar y asignar el día fijo
        dia_fijo = request.form.get('dia_descanso_fijo')
        empleado.dia_descanso_fijo = int(dia_fijo) if dia_fijo else None

        fid = request.form['farmacia_id']
        empleado.farmacia_id = None if fid == "" else int(fid)
        empleado.username = request.form['username']
        
        if request.form['password']: # Solo actualiza clave si no está en blanco
            empleado.password = request.form['password']
            
        db.session.commit()
        return redirect(url_for('admin_dashboard'))
        
    farmacias = Farmacia.query.all()
    return render_template('empleado_form.html', farmacias=farmacias, empleado=empleado)

@app.route('/admin/empleado/eliminar/<int:id>')
def eliminar_empleado(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    empleado = Empleado.query.get(id)
    if empleado:
        db.session.delete(empleado)
        db.session.commit()
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

        farm_list = [{'id': f.id, 'nombre': f.nombre} for f in farmacias_db]

        # AUSENCIAS APROBADAS
        aprobadas = Solicitud.query.filter(
            Solicitud.estado.in_(['Aprobada', 'Modificada (Aprobada)'])
        ).all()

        permisos_por_empleado = {}
        for s in aprobadas:
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

        emp_list = []
        for e in empleados_db:
            dia_descanso = e.dia_descanso_fijo
            if dia_descanso is None and e.id in descanso_rotativo:
                dia_descanso = descanso_rotativo[e.id]
            emp_list.append({
                'id': e.id,
                'rol': e.rol,
                'farmacia_id': e.farmacia_id,
                'dia_descanso_fijo': dia_descanso
            })

        ausencias_lista = []
        for s in aprobadas:
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

        resultados = generar_horario_semana(
            emp_list,
            farm_list,
            dias=7,
            ausencias=ausencias_lista,
            forzadas=asignaciones_forzadas
        )

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
                    if perm.mensaje_admin == 'Registro Administrativo Directo':
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
    if solicitud and solicitud.mensaje_admin == 'Registro Administrativo Directo':
        flash('No tienes permiso para cancelar registros administrativos.', 'error')
        return redirect(url_for('empleado_dashboard'))
        
    if solicitud and solicitud.empleado_id == session['user_id']:
        solicitud.estado = 'Pide Cancelación'
        db.session.commit()
        flash('Solicitud de cancelación enviada al administrador.', 'success')
        
    return redirect(url_for('empleado_dashboard'))

@app.route('/empleado/solicitud/nueva', methods=['POST'])
def nueva_solicitud_empleado():
    if 'user_id' not in session or session.get('rol') == 'Admin':
        return jsonify({'status': 'error', 'mensaje': 'unauthorized'}), 401

    fecha = request.form.get('fecha')
    motivo = request.form.get('motivo')

    if not fecha or not motivo:
        return jsonify({'status': 'error', 'mensaje': 'Debes completar la fecha y el motivo.'}), 400

    nueva = Solicitud(
        empleado_id=session['user_id'],
        fecha=fecha,
        motivo=motivo,
        estado='Pendiente',
        mensaje_admin=''
    )

    db.session.add(nueva)
    db.session.commit()
    return jsonify({'status': 'ok'})


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
            'mensaje_admin': s.mensaje_admin or ''
        })

    return jsonify({'status': 'ok', 'solicitudes': payload})

@app.route('/admin/solicitudes/confirmar_cancelacion/<int:id>')
def confirmar_cancelacion(id):
    if 'user_id' not in session or session.get('rol') != 'Admin':
        return redirect(url_for('login'))
        
    solicitud = Solicitud.query.get(id)
    if solicitud and solicitud.estado == 'Pide Cancelación':
        solicitud.estado = 'Cancelada'
        db.session.commit()

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'ok', 'mensaje': 'Solicitud cancelada'})
        return redirect(url_for('admin_solicitudes'))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'error', 'mensaje': 'Solicitud no encontrada'}), 404
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

with app.app_context():
    db.create_all()
    seed_data()

if __name__ == '__main__':
    app.run(debug=True, port=5000)