{
  description = "Dev Nix Flake for local development";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils = {
      url = "github:numtide/flake-utils";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    devshell = {
      url = "github:numtide/devshell";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        workspaceRoot = ./.;
        venvName = "venv";

        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
        python = pkgs.python314;
        workspace = inputs.uv2nix.lib.workspace.loadWorkspace { workspaceRoot = workspaceRoot; };
        overlay = workspace.mkPyprojectOverlay {
          sourcePreference = "wheel";
        };
        # Fix rouge-score missing setuptools build dependency
        rougeScoreOverlay = final: prev: {
          rouge-score = prev.rouge-score.overrideAttrs (old: {
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ prev.setuptools ];
          });
        };
        baseSet = pkgs.callPackage inputs.pyproject-nix.build.packages {
          python = python;
        };
        pythonSet = baseSet.overrideScope (
          pkgs.lib.composeManyExtensions [
            inputs.pyproject-build-systems.overlays.default
            overlay
            rougeScoreOverlay
          ]
        );
        venv = pythonSet.mkVirtualEnv "${venvName}" workspace.deps.default;
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            # Base
            pkgs.envsubst
            pkgs.bashInteractive

            # Python
            pkgs.uv
            venv
            pkgs.poetry

            # NodeJS
            pkgs.nodejs_22

            # C++ standard library for numpy
            pkgs.stdenv.cc.cc.lib

            # Kubernetes
            pkgs.k3d
            pkgs.kubernetes-helm
            pkgs.kubectl
          ];

          # Make libstdc++.so.6 available to uv's .venv
          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"
          '';
        };
      }
    );
}
