

def pytest_configure(config):
    config.addinivalue_line("markers", "chaos: chaos engineering tests")
