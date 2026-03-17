lse "application_modules"
    )

    with open(boot_up_file, "w") as file:
        json.dump(boot_up_data, file)