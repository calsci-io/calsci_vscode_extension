 data
        self.flush()

        # Let the storage clean up too
        self.storage.close()