import sys
import os
import ast
import json

try:
    import networkx as nx
    from pyvis.network import Network
except ImportError:
    print("Please install requirements: pip install networkx pyvis")
    sys.exit(1)

def build_knowledge_graph(root_dir, output_file="knowledge_graph.html"):
    G = nx.DiGraph()
    
    # Track files and their contents
    for foldername, subfolders, filenames in os.walk(root_dir):
        # Skip certain directories
        if any(exclude in foldername for exclude in ['__pycache__', 'venv', '.git', 'backtest_results', 'logs', 'graphify']):
            continue
            
        for filename in filenames:
            if filename.endswith(".py"):
                filepath = os.path.join(foldername, filename)
                rel_path = os.path.relpath(filepath, root_dir)
                mod_name = rel_path.replace(os.sep, ".").replace(".py", "")
                
                G.add_node(mod_name, group="module", title=f"Module: {mod_name}", label=mod_name, size=20)
                
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=filepath)
                except Exception as e:
                    print(f"Failed to parse {filepath}: {e}")
                    continue
                
                for node in ast.walk(tree):
                    # Classes
                    if isinstance(node, ast.ClassDef):
                        class_id = f"{mod_name}.{node.name}"
                        G.add_node(class_id, group="class", title=f"Class: {node.name}", label=node.name, color="#ff9999", size=15)
                        G.add_edge(mod_name, class_id, title="defines")
                        
                        # Methods inside classes
                        for class_node in node.body:
                            if isinstance(class_node, ast.FunctionDef):
                                func_id = f"{class_id}.{class_node.name}"
                                G.add_node(func_id, group="method", title=f"Method: {class_node.name}", label=class_node.name, color="#99ff99", size=10)
                                G.add_edge(class_id, func_id, title="contains")
                                
                    # Top-level Functions
                    elif isinstance(node, ast.FunctionDef):
                        # Avoid adding methods twice (ast.walk visits them again, so we filter top-level)
                        if getattr(node, 'is_method', False):
                            continue
                        func_id = f"{mod_name}.{node.name}"
                        G.add_node(func_id, group="function", title=f"Function: {node.name}", label=node.name, color="#99ccff", size=10)
                        G.add_edge(mod_name, func_id, title="defines")
                        
                    # Imports
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            imported_mod = alias.name
                            G.add_node(imported_mod, group="external", title=f"Import: {imported_mod}", size=15, color="#e0e0e0")
                            G.add_edge(mod_name, imported_mod, title="imports")
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            imported_mod = node.module
                            G.add_node(imported_mod, group="external", title=f"Import: {imported_mod}", size=15, color="#e0e0e0")
                            G.add_edge(mod_name, imported_mod, title="imports from")

    # Generate JSON output
    data = nx.node_link_data(G)
    json_output_file = output_file.replace(".html", ".json")
    with open(json_output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"JSON Knowledge graph successfully generated: {json_output_file}")

    # Generate Pyvis Network
    print(f"Building knowledge graph with {len(G.nodes)} nodes and {len(G.edges)} edges...")
    net = Network(height="1000px", width="100%", bgcolor="#222222", font_color="white", directed=True)
    net.from_nx(G)
    
    # Add physics for better visualization
    net.force_atlas_2based(gravity=-50, central_gravity=0.01, spring_length=100, spring_strength=0.08, damping=0.4, overlap=0)
    net.show_buttons(filter_=['physics'])
    
    net.write_html(output_file)
    print(f"Knowledge graph successfully generated: {output_file}")


if __name__ == "__main__":
    # Point project root to the parent directory of this script's location
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(base_dir)
    output_html = os.path.join(base_dir, "codebase_knowledge_graph.html")
    
    # Exclude graphify directory from parsing to avoid self-referencing
    build_knowledge_graph(project_root, output_html)
