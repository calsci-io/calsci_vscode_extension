_str = f"{app.get_app_name()}()"
    
    app.set_none()

    exec(str(imp_str))
    exec(str(run_str))