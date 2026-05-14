from ortools.sat.python import cp_model

def generar_horario_semana(empleados, farmacias, dias=7, ausencias=None, forzadas=None):
    if ausencias is None:
        ausencias = []
    if forzadas is None:
        forzadas = []

    # Solo modelamos variables para empleados/farmacias presentes en las listas.
    # Ausencias y asignaciones forzadas pueden referir a empleados excluidos del motor
    # (p. ej. sin horario definido) o farmacias eliminadas; ignorarlas evita KeyError.
    valid_e_ids = {e['id'] for e in empleados}
    valid_f_ids = {f['id'] for f in farmacias}

    def _dia_ok(d):
        return isinstance(d, int) and 0 <= d < dias

    ausencias = [
        a for a in ausencias
        if a.get('empleado_id') in valid_e_ids and _dia_ok(a.get('dia'))
    ]
    forzadas = [
        o for o in forzadas
        if o.get('empleado_id') in valid_e_ids
        and o.get('farmacia_dest_id') in valid_f_ids
        and _dia_ok(o.get('dia'))
    ]

    model = cp_model.CpModel()
    farmacias_domingo = ["San josé Parque", "Amatitlan", "Alioto"]

    # 1. Variables de turnos
    shifts = {}
    for e in empleados:
        for f in farmacias:
            for d in range(dias):
                shifts[(e['id'], f['id'], d)] = model.NewBoolVar(f"shift_e{e['id']}_f{f['id']}_d{d}")

    # R1: Un empleado solo puede trabajar en una farmacia por día (o ninguna)
    for e in empleados:
        for d in range(dias):
            model.Add(sum(shifts[(e['id'], f['id'], d)] for f in farmacias) <= 1)

    # R2: Máximo 6 días por semana por empleado
    # Cumplimiento Art. 126 Código de Trabajo Guatemala - Descanso semanal obligatorio
    for e in empleados:
        total_dias = sum(shifts[(e['id'], f['id'], d)] for f in farmacias for d in range(dias))
        model.Add(total_dias <= 6)
        # El >= 1 se maneja solo por el objetivo Maximize,
        # no es una restricción dura porque puede haber semanas
        # donde el empleado tenga permiso toda la semana.

    # R3: Permisos Aprobados (El empleado que pidió permiso NO trabaja ese día)
    for aus in ausencias:
        for f in farmacias:
            model.Add(shifts[(aus['empleado_id'], f['id'], aus['dia'])] == 0)

    # R4: Cierres en domingo (Nadie trabaja en farmacias cerradas en domingo)
    for f in farmacias:
        if f.get('nombre') not in farmacias_domingo:
            for e in empleados:
                model.Add(shifts[(e['id'], f['id'], 6)] == 0)

    # R5: Asignaciones Forzadas vs Farmacia Base (La regla más delicada)
    for e in empleados:
        if e['rol'] == 'Dependiente' and e['farmacia_id']:
            for d in range(dias):
                
                # Ver si el admin le forzó una farmacia específica a este empleado HOY
                orden_hoy = None
                for of in forzadas:
                    if of['empleado_id'] == e['id'] and of['dia'] == d:
                        orden_hoy = of
                        break

                if orden_hoy:
                    # El admin OBLIGA a que el empleado esté en esta farmacia destino hoy
                    for f in farmacias:
                        if f['id'] == orden_hoy['farmacia_dest_id']:
                            # Obligamos a que el turno sea 1, EXCEPTO si es domingo y la farmacia cierra.
                            # Para evitar conflicto con la R4
                            if d == 6 and f.get('nombre') not in farmacias_domingo:
                                model.Add(shifts[(e['id'], f['id'], d)] == 0) # El domingo pesa más
                            else:
                                tiene_ausencia = any(
                                    a['empleado_id'] == e['id'] and a['dia'] == d
                                    for a in ausencias
                                )
                                if not tiene_ausencia:
                                    model.Add(shifts[(e['id'], f['id'], d)] == 1)
                                else:
                                    # Si hay ausencia, respetar R3 y no forzar
                                    model.Add(shifts[(e['id'], f['id'], d)] == 0)
                        else:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)
                else:
                    # Si NO hay orden del admin, el dependiente solo puede estar en su farmacia base
                    for f in farmacias:
                        if f['id'] != e['farmacia_id']:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)


    # R6: Lógica de Comodines (A prueba de fallos)
    # Extraemos dónde y cuándo faltará alguien (los huecos a cubrir)
    huecos_aprobados = []
    for a in ausencias:
        fid = a.get('farmacia_id')
        if fid is not None and fid in valid_f_ids:
            huecos_aprobados.append((fid, a['dia']))

    for e in empleados:
        if e['rol'] == 'Comodin':
            for d in range(dias):
                
                # Ver si el admin OBLIGÓ al comodín a ir a algún lado hoy
                orden_comodin_hoy = None
                for of in forzadas:
                    if of['empleado_id'] == e['id'] and of['dia'] == d:
                        orden_comodin_hoy = of
                        break

                for f in farmacias:
                    if orden_comodin_hoy:
                        # Si el admin lo ordenó, el comodín TIENE que ir a esa farmacia (salvo cierre en domingo)
                        if f['id'] == orden_comodin_hoy['farmacia_dest_id']:
                            if d == 6 and f.get('nombre') not in farmacias_domingo:
                                model.Add(shifts[(e['id'], f['id'], d)] == 0)
                            else:
                                tiene_ausencia_comodin = any(
                                    a['empleado_id'] == e['id'] and a['dia'] == d
                                    for a in ausencias
                                )
                                if not tiene_ausencia_comodin:
                                    model.Add(shifts[(e['id'], f['id'], d)] == 1)
                                else:
                                    model.Add(shifts[(e['id'], f['id'], d)] == 0)
                        else:
                            model.Add(shifts[(e['id'], f['id'], d)] == 0)
                    else:
                        # Si NO hay orden del admin, el comodín SOLO puede cubrir permisos médicos/personales
                        # No agregar restricción == 0 cuando no hay huecos.
                        # Solo bloquear si hay huecos en OTRAS farmacias ese día
                        # (para que no cubra dos lugares a la vez, ya lo controla R1).
                        pass

    # R7: Máximo 1 comodín por hueco (Para no mandar a todos a cubrir el mismo lugar)
    # Solo aplica para los comodines sin órdenes forzadas.
    comodines = [e for e in empleados if e['rol'] == 'Comodin']
    if comodines:
        for (f_id, d) in huecos_aprobados:
            model.Add(sum(shifts[(c['id'], f_id, d)] for c in comodines) <= 1)

    # R8: Descanso fijo programado (Ej. Universidad o Religión)
    # Cumplimiento Art. 126 Código de Trabajo Guatemala - Descanso semanal obligatorio
    for e in empleados:
        d_fijo = e.get('dia_descanso_fijo')
        if d_fijo is not None and _dia_ok(d_fijo):
            for f in farmacias:
                # El modelo fuerza a que el turno en ese día específico sea 0
                model.Add(shifts[(e['id'], f['id'], d_fijo)] == 0)

    # OBJETIVO: Maximizar la cobertura total
    cobertura_total = []
    for f in farmacias:
        for d in range(dias):
            # No contamos las farmacias cerradas en domingo para el objetivo
            if d == 6 and f.get('nombre') not in farmacias_domingo:
                continue
            cobertura_total.append(sum(shifts[(e['id'], f['id'], d)] for e in empleados))

    model.Maximize(sum(cobertura_total))

    # Ejecutar Solver
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5.0

    print(f"[OR-Tools] Empleados: {len(empleados)}")
    print(f"[OR-Tools] Ausencias: {ausencias}")
    print(f"[OR-Tools] Forzadas: {forzadas}")
    print(f"[OR-Tools] Huecos aprobados: {huecos_aprobados}")
    for e in empleados:
        dias_bloqueados = [a['dia'] for a in ausencias if a['empleado_id'] == e['id']]
        descanso = e.get('dia_descanso_fijo')
        print(f"  {e['nombre']} | descanso_fijo={descanso} | ausencias={dias_bloqueados}")

    status = solver.Solve(model)
    print("STATUS OR-TOOLS:", status) # Esto te ayudará en la consola

    if status not in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
        print("No se encontró solución factible.")
        return None

    # Guardar resultados
    resultados = []
    for e in empleados:
        for f in farmacias:
            for d in range(dias):
                if solver.Value(shifts[(e['id'], f['id'], d)]) == 1:
                    resultados.append({
                        'empleado_id': e['id'],
                        'farmacia_id': f['id'],
                        'dia': d
                    })

    return resultados