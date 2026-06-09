"""
setup_database.py - Initialize the database with sample data.
Run this script once (or to reset) before starting the app.
"""

from app import app, db, Team, Employee

def setup_database():
    with app.app_context():
        print("Creating database tables...")
        db.create_all()

        existing_teams = Team.query.count()
        if existing_teams > 0:
            print("Database already contains data!")
            response = input("Reset the database? (yes/no): ")
            if response.lower() != 'yes':
                print("Setup cancelled.")
                return
            print("Dropping all tables...")
            db.drop_all()
            db.create_all()

        # Teams
        print("\nCreating teams...")
        teams_data = ['Redrockers', 'Sales Team', 'Support Team', 'Development Team']
        teams = []
        for name in teams_data:
            t = Team(name=name)
            db.session.add(t)
            teams.append(t)
            print(f"  ✓ {name}")
        db.session.commit()

        # Admin
        print("\nCreating admin user...")
        admin = Employee(name='Admin User', email='admin@example.com', password='admin123', is_admin=True)
        db.session.add(admin)
        db.session.flush()
        admin.teams.append(teams[0])

        # Sample employees (some assigned to multiple teams)
        print("\nCreating sample employees...")
        samples = [
            ('Kiran Kumar',   'kiran@example.com',  'password123', [0]),
            ('Priya Sharma',  'priya@example.com',  'password123', [0, 1]),
            ('Raj Patel',     'raj@example.com',    'password123', [0]),
            ('Sarah Johnson', 'sarah@example.com',  'password123', [1, 2]),
            ('Mike Chen',     'mike@example.com',   'password123', [1]),
        ]
        for name, email, password, team_indices in samples:
            emp = Employee(name=name, email=email, password=password, is_admin=False)
            db.session.add(emp)
            db.session.flush()
            for i in team_indices:
                emp.teams.append(teams[i])
            print(f"  ✓ {name} ({email}) → teams: {[teams[i].name for i in team_indices]}")

        db.session.commit()

        print("\n" + "=" * 50)
        print("Database setup complete!")
        print("=" * 50)
        print("\nLogin with:")
        print("  Email:    admin@example.com")
        print("  Password: admin123")
        print("=" * 50)

if __name__ == '__main__':
    setup_database()
