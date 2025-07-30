import sys
import time
import json
import os
import subprocess
import argparse
import requests
from antlr4 import *
from TerraformSubsetLexer import TerraformSubsetLexer
from TerraformSubsetParser import TerraformSubsetParser
from TerraformSubsetListener import TerraformSubsetListener

class TerraformApplyListener(TerraformSubsetListener):
    def __init__(self):
        self.variables = {}
        self.provider_token_expr = None  # store raw expression (e.g., var.token)
        self.droplet_config = {}
        self.droplet_id = None
        self.droplet_ip = None

    def enterVariable(self, ctx):
        var_name = ctx.STRING().getText().strip('"')
        for kv in ctx.body().keyValue():
            key = kv.IDENTIFIER().getText()
            if key == "default":
                value = kv.expr().getText().strip('"')
                self.variables[var_name] = value
                #print(f"[var] {var_name} = {value}")

    def enterProvider(self, ctx):
        provider_name = ctx.STRING().getText().strip('"')
        if provider_name != "digitalocean":
            raise Exception("Only 'digitalocean' provider is supported.")

        for kv in ctx.body().keyValue():
            key = kv.IDENTIFIER().getText()
            expr = kv.expr().getText()
            if key == "token":
                self.provider_token_expr = expr  # store raw expr for now

    def enterResource(self, ctx):
        type_ = ctx.STRING(0).getText().strip('"')
        name = ctx.STRING(1).getText().strip('"')
        if type_ != "digitalocean_droplet":
            return

        for kv in ctx.body().keyValue():
            key = kv.IDENTIFIER().getText()
            val = kv.expr().getText().strip('"')
            self.droplet_config[key] = val

    def resolve_token(self):
        if not self.provider_token_expr:
            raise Exception("No token specified in provider block.")
        if self.provider_token_expr.startswith("var."):
            var_name = self.provider_token_expr.split(".")[1]
            if var_name in self.variables:
                return self.variables[var_name]
            else:
                raise Exception(f"Undefined variable '{var_name}' used in provider block.")
        return self.provider_token_expr.strip('"')



def create_droplet(api_token, config):
    """Create a DigitalOcean droplet"""
    url = "https://api.digitalocean.com/v2/droplets"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}"
    }

    payload = {
        "name": config["name"],
        "region": config["region"],
        "size": config["size"],
        "image": config["image"],
        "ssh_keys": [],
        "backups": False,
        "ipv6": False,
        "user_data": None,
        "private_networking": None,
        "volumes": None,
        "tags": []
    }

    print("[*] Creating droplet...")
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    droplet = response.json()["droplet"]
    droplet_id = droplet["id"]
    print(f"[+] Droplet created with ID: {droplet_id}")

    print("[*] Waiting for droplet to become active and assigned an IP...")
    while True:
        resp = requests.get(f"https://api.digitalocean.com/v2/droplets/{droplet_id}", headers=headers)
        droplet_info = resp.json()["droplet"]
        networks = droplet_info["networks"]["v4"]
        public_ips = [n["ip_address"] for n in networks if n["type"] == "public"]
        if public_ips:
            return droplet_id, public_ips[0]
        time.sleep(5)

def delete_droplet_api(api_token, droplet_id):
    """Delete droplet using REST API"""
    url = f"https://api.digitalocean.com/v2/droplets/{droplet_id}"
    headers = {
        "Authorization": f"Bearer {api_token}"
    }
    
    print(f"[*] Deleting droplet {droplet_id} via REST API...")
    response = requests.delete(url, headers=headers)
    
    if response.status_code == 204:
        print(f"[✓] Droplet {droplet_id} deleted successfully")
        return True
    else:
        print(f"[!] Failed to delete droplet {droplet_id}: {response.status_code}")
        return False



def save_statefile(droplet_info, filename="terraform.tfstate"):
    """Create and save a terraform statefile"""
    statefile_content = {
        "version": 4,
        "terraform_version": "1.0.0",
        "serial": 1,
        "lineage": "terraform-parser-generated",
        "outputs": {},
        "resources": [
            {
                "mode": "managed",
                "type": "digitalocean_droplet",
                "name": "example",
                "provider": "provider[\"registry.terraform.io/digitalocean/digitalocean\"]",
                "instances": [
                    {
                        "schema_version": 1,
                        "attributes": {
                            "id": str(droplet_info["id"]),
                            "name": droplet_info["name"],
                            "region": droplet_info["region"],
                            "size": droplet_info["size"],
                            "image": droplet_info["image"],
                            "ipv4_address": droplet_info["ip"],
                            "status": "active",
                            "created_at": droplet_info.get("created_at", ""),
                            "tags": droplet_info.get("tags", [])
                        }
                    }
                ]
            }
        ]
    }
    
    try:
        with open(filename, 'w') as f:
            json.dump(statefile_content, f, indent=2)
        print(f"[✓] Terraform statefile saved: {filename}")
        return True
    except Exception as e:
        print(f"[!] Failed to save statefile: {e}")
        return False

def save_droplet_info_json(droplet_info, filename="droplet_info.json"):
    """Save droplet information to JSON file"""
    try:
        with open(filename, 'w') as f:
            json.dump(droplet_info, f, indent=2)
        print(f"[✓] Droplet info saved to: {filename}")
        return True
    except Exception as e:
        print(f"[!] Failed to save droplet info: {e}")
        return False

def run_terraform_commands():
    """Run standard terraform commands"""
    commands = [
        ["terraform", "init"],
        ["terraform", "plan"],
    ]
    
    for cmd in commands:
        try:
            print(f"[*] Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            
            if result.returncode == 0:
                print(f"[✓] {cmd[1]} completed successfully")
                if result.stdout:
                    print(result.stdout)
            else:
                print(f"[!] {cmd[1]} failed:")
                print(result.stderr)
                return False
        except Exception as e:
            print(f"[!] Error running {' '.join(cmd)}: {e}")
            return False
    
    return True

def main():
    parser = argparse.ArgumentParser(description='Enhanced Terraform Parser with full lifecycle management')
    parser.add_argument('terraform_file', help='Path to the Terraform file')
    parser.add_argument('--action', choices=['apply', 'destroy', 'plan'], default='apply',
                       help='Action to perform (default: apply)')

    
    args = parser.parse_args()

    try:
        # Parse Terraform file
        input_stream = FileStream(args.terraform_file)
        lexer = TerraformSubsetLexer(input_stream)
        stream = CommonTokenStream(lexer)
        terraform_parser = TerraformSubsetParser(stream)
        tree = terraform_parser.terraform()

        listener = TerraformApplyListener()
        walker = ParseTreeWalker()
        walker.walk(listener, tree)

        token = listener.resolve_token()
        if not listener.droplet_config:
            raise Exception("Missing digitalocean_droplet resource.")

        if args.action == 'apply':
            # Create droplet
            droplet_id, ip = create_droplet(token, listener.droplet_config)
            listener.droplet_id = droplet_id
            listener.droplet_ip = ip
            
            print(f"[✓] Droplet available at IP: {ip}")
            print(f"[✓] Droplet ID: {droplet_id}")

            # Save droplet information
            droplet_info = {
                "id": droplet_id,
                "ip": ip,
                "name": listener.droplet_config["name"],
                "region": listener.droplet_config["region"],
                "size": listener.droplet_config["size"],
                "image": listener.droplet_config["image"],
                "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "tags": []
            }
            
            # Save to JSON
            save_droplet_info_json(droplet_info)
            
            # Create terraform statefile
            save_statefile(droplet_info)
            
            # Run terraform commands
            print("\n[*] Running terraform ecosystem commands...")
            run_terraform_commands()
            
        elif args.action == 'destroy':
            # For destroy, we need the droplet ID from a previous run or statefile
            if os.path.exists('droplet_info.json'):
                with open('droplet_info.json', 'r') as f:
                    droplet_info = json.load(f)
                    droplet_id = droplet_info['id']
                    
                # Delete using API
                delete_droplet_api(token, droplet_id)
                    
                # Clean up files
                for file in ['droplet_info.json', 'terraform.tfstate']:
                    if os.path.exists(file):
                        os.remove(file)
                        print(f"[✓] Cleaned up: {file}")
            else:
                print("[!] No droplet info found. Cannot destroy.")
                
        elif args.action == 'plan':
            print("[*] Plan mode - showing what would be created:")
            print(f"[+] Would create droplet with config: {listener.droplet_config}")
            run_terraform_commands()

    except Exception as e:
        print(f"[!] Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()