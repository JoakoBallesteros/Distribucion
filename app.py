import os
import unicodedata
import json
import pandas as pd
from io import BytesIO
from datetime import datetime, time, timedelta
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_file, session
)

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key')

# Carpetas de entrada/salida
UPLOAD_FOLDER = os.path.abspath(os.path.dirname(__file__))
OUTPUT_FOLDER = os.path.join(UPLOAD_FOLDER, 'outputs')
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Constantes de negocio
IDEAL_PER_LEADER = 22
WORK_HOURS       = 7
ASSIGN_WINDOW    = 3

def to_time(val):
    if pd.isna(val):
        return None
    if isinstance(val, time):
        return val
    if isinstance(val, datetime):
        return val.time()
    if isinstance(val, str):
        for fmt in ('%H:%M:%S','%H:%M'):
            try:
                return datetime.strptime(val.strip(), fmt).time()
            except:
                pass
    if isinstance(val, (int, float)):
        try:
            base = datetime(1899, 12, 30)
            return (base + timedelta(days=float(val))).time()
        except:
            return None
    return None

def normalize(text):
    s = str(text).strip().lower()
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c))

@app.route('/', methods=['GET','POST'])
def upload():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f:
            return render_template('upload.html', error='Debes subir un archivo')
        filename = f.filename
        path     = os.path.join(UPLOAD_FOLDER, filename)
        f.save(path)
        return redirect(url_for('select', filename=filename))
    return render_template('upload.html')

@app.route('/select/<filename>', methods=['GET','POST'])
def select(filename):
    # Leo el archivo subido
    path = os.path.join(UPLOAD_FOLDER, filename)
    if filename.lower().endswith(('.xls','.xlsx')):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    df.columns = df.columns.str.strip()

    # Filtro por JEFE
    boss_norm      = normalize('ARIANA MICAELA ALTURRIA')
    df['JEFE_NORM'] = df['JEFE'].apply(normalize)
    df = df[df['JEFE_NORM'] == boss_norm].drop(columns=['JEFE_NORM'])

    # Columnas clave + conversión de INGRESO
    df = df[['SUPERIOR','NOMBRE','INGRESO','SERVICIO']].dropna(subset=['SUPERIOR'])
    df['INGRESO_TIME'] = df['INGRESO'].apply(to_time)

    leaders      = sorted(df['SUPERIOR'].unique())
    all_services = sorted(df['SERVICIO'].dropna().unique())
    time_options = [f"{h:02d}:00" for h in range(24)]

    if request.method == 'POST':
        # Recojo configuraciones
        start_times     = {L: request.form.get(f'start_{L}')       for L in leaders}
        leader_services = {L: request.form.getlist(f'service_{L}') for L in leaders}

        # Calculo ventanas de asignación
        assign_bounds = {}
        for L, st in start_times.items():
            if not st:
                continue
            t0   = datetime.strptime(st, '%H:%M').time()
            base = datetime.combine(datetime.today(), t0)
            assign_bounds[L] = (
                t0,
                (base + timedelta(hours=ASSIGN_WINDOW)).time()
            )

        # Inicializo estructuras de asignación
        assignments   = {L: [] for L in leaders}
        total_count   = {L: 0   for L in leaders}
        service_count = {L: {}  for L in leaders}
        rr_counters   = {}

        # Ordeno reps por frecuencia de servicio ascendente
        freq       = df['SERVICIO'].value_counts().to_dict()
        df_sorted  = df.copy()
        df_sorted['FREQ'] = df_sorted['SERVICIO'].map(freq)
        df_sorted.sort_values(['FREQ','SERVICIO'], inplace=True)

        # Asigno reps a líderes
        for _, row in df_sorted.iterrows():
            rep = row['NOMBRE']
            svc = row['SERVICIO']
            rt  = row['INGRESO_TIME']
            if rt is None:
                continue
            turno = rt.strftime('%H:%M')

            # Filtrar líderes por ventana y servicio
            cands = []
            for L,(start_t,end_t) in assign_bounds.items():
                if svc not in leader_services[L]:
                    continue
                if start_t <= end_t:
                    if start_t <= rt <= end_t:
                        cands.append(L)
                else:
                    if rt >= start_t or rt <= end_t:
                        cands.append(L)
            if not cands:
                continue

            # Prefiltro ideal
            under = [L for L in cands if total_count[L] < IDEAL_PER_LEADER]
            pool  = under if under else cands

            # Round‐robin por (servicio, turno)
            key   = (svc, turno)
            idx   = rr_counters.get(key, 0)
            chosen= pool[idx % len(pool)]
            rr_counters[key] = idx + 1

            # Registro
            assignments[chosen].append({
                'rep'     : rep,
                'service' : svc,
                'ingreso' : turno
            })
            total_count[chosen]       += 1
            service_count[chosen].setdefault(svc, 0)
            service_count[chosen][svc] += 1

        # Genero el Excel en memoria
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            # Hoja "Distribuciones"
            rows = []
            for L, infos in assignments.items():
                for info in infos:
                    rows.append({
                        'Líder'   : L,
                        'Ingreso' : info['ingreso'],
                        'Servicio': info['service'],
                        'Rep'     : info['rep']
                    })
            pd.DataFrame(rows).to_excel(writer, index=False, sheet_name='Distribuciones')
            # Hoja "Resumen"
            resumen = []
            for L, infos in assignments.items():
                resumen.append({
                    'Líder'     : L,
                    'Ideal'     : IDEAL_PER_LEADER,
                    'Asignados' : len(infos),
                    'Diferencia': len(infos) - IDEAL_PER_LEADER
                })
            pd.DataFrame(resumen).to_excel(writer, index=False, sheet_name='Resumen')
        output.seek(0)
        with open(os.path.join(OUTPUT_FOLDER,'Distribuciones.xlsx'),'wb') as f:
            f.write(output.getvalue())

        # Ordeno líderes por hora de inicio
        ordered_leaders = sorted(
            leaders,
            key=lambda L: datetime.strptime(start_times[L], '%H:%M')
        )

        # Renderizo la vista de resultados
        return render_template(
            'results.html',
            assignments=assignments,
            ordered_leaders=ordered_leaders,
            file_name='Distribuciones.xlsx'
        )

    # GET: cargo configuración previa y filename
    selected_times    = session.get('start_times', {})
    selected_services = session.get('leader_services', {})

    return render_template(
        'select.html',
        filename=filename,
        leaders=leaders,
        time_options=time_options,
        services=all_services,
        selected_times=selected_times,
        selected_services=selected_services
    )

@app.route('/reassign/<filename>', methods=['POST'])
def reassign(filename):
    raw        = request.form.get('reassignments', '{}')
    new_assign = json.loads(raw)
    uploaded   = session.get('uploaded_file', filename)
    path       = os.path.join(UPLOAD_FOLDER, uploaded)
    if uploaded.lower().endswith(('.xls','.xlsx')):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    df.columns = df.columns.str.strip()
    df = df[df['JEFE'].apply(normalize)=='ariana micaela alturria']
    df = df[['SUPERIOR','NOMBRE','INGRESO','SERVICIO']]

    assignments = {L: [] for L in new_assign}
    lookup      = df.set_index('NOMBRE').to_dict('index')
    for L, reps in new_assign.items():
        for rep in reps:
            if rep in lookup:
                entry = lookup[rep]
                ing   = to_time(entry['INGRESO'])
                assignments[L].append({
                    'rep'     : rep,
                    'service' : entry['SERVICIO'],
                    'ingreso' : ing.strftime('%H:%M') if ing else ''
                })

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        rows = []
        for L, infos in assignments.items():
            for info in infos:
                rows.append({
                    'Líder'   : L,
                    'Ingreso' : info['ingreso'],
                    'Servicio': info['service'],
                    'Rep'     : info['rep']
                })
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name='Distribuciones')
        resumen = []
        for L, infos in assignments.items():
            resumen.append({
                'Líder'     : L,
                'Ideal'     : IDEAL_PER_LEADER,
                'Asignados' : len(infos),
                'Diferencia': len(infos) - IDEAL_PER_LEADER
            })
        pd.DataFrame(resumen).to_excel(writer, index=False, sheet_name='Resumen')
    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name='Distribuciones.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/download/<file_name>')
def download(file_name):
    return send_file(
        os.path.join(OUTPUT_FOLDER, file_name),
        as_attachment=True,
        download_name=file_name,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

if __name__ == '__main__':
    app.run(debug=True)
