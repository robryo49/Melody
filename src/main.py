import os

# GTK's Vulkan renderer spams "VK_SUBOPTIMAL_KHR" warnings on some
# compositors. Use the OpenGL renderer instead. This must be set before GTK
# is imported, and setdefault lets `GSK_RENDERER=vulkan` still override it.
os.environ.setdefault("GSK_RENDERER", "gl")

from melody import main

if __name__ == "__main__":
    main()
